"""Steps 1-3 — case registration, appearance generation and face signatures; plus case closure."""
import random
from datetime import datetime, timezone

import numpy as np
from fastapi import HTTPException

from . import db, variations
from .config import MAX_GENERATED_LOOKS, MIN_VARIANT_SELF_SIM
from .face_engine import decode_image, encode_jpeg, engine, portrait_crop
from .security import audit, delete_blob, store_blob
from .watchlist import watchlist


def _parse_date(value: str) -> datetime:
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "missing_since must be a date like 2026-09-01")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _add_photo(case_id: int, kind: str, label: str, img: np.ndarray, embedding: np.ndarray, age=None) -> int:
    blob = store_blob(encode_jpeg(img))
    photo_id = db.execute(
        "INSERT INTO photos (case_id, kind, label, age_at_photo, blob, created_at) VALUES (?,?,?,?,?,?)",
        (case_id, kind, label, age, blob, db.now()),
    )
    db.execute(
        "INSERT INTO signatures (case_id, photo_id, kind, embedding) VALUES (?,?,?,?)",
        (case_id, photo_id, kind, embedding.astype(np.float32).tobytes()),
    )
    return photo_id


def register_case(fields: dict, photos: list[tuple[bytes, float | None, str]], user: dict) -> dict:
    if not fields.get("guardian_consent"):
        raise HTTPException(400, "Guardian consent is required to activate a case")
    if not (fields.get("gd_number") or "").strip():
        raise HTTPException(400, "A police case / GD number is required (prevents misuse)")
    if not photos:
        raise HTTPException(400, "At least one photo of the child is required")

    missing_since = _parse_date(fields["missing_since"])
    days_missing = max(0.0, (datetime.now(timezone.utc) - missing_since).total_seconds() / 86400)

    year = datetime.now().year
    seq = db.one("SELECT COUNT(*) AS n FROM cases")["n"] + 1
    case_code = f"SA-{year}-{seq:04d}"
    case_id = db.execute(
        """INSERT INTO cases (case_code, child_name, gender, age_at_missing, missing_since, last_seen_location,
           gd_number, police_station, guardian_name, guardian_phone, guardian_consent, case_officer,
           case_officer_phone, notes, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            case_code, fields.get("child_name"), fields.get("gender"), fields.get("age_at_missing"),
            missing_since.date().isoformat(), fields.get("last_seen_location"), fields["gd_number"].strip(),
            fields["police_station"], fields.get("guardian_name"), fields.get("guardian_phone"), 1,
            fields.get("case_officer"), fields.get("case_officer_phone"), fields.get("notes"),
            db.now(), user["username"],
        ),
    )

    # ---- Step 1: real photos -> anchor signatures
    fe = engine()
    anchors, rejected = [], []
    for data, age, label in photos:
        img = decode_image(data)
        face = fe.largest_face(img) if img is not None else None
        if face is None:
            rejected.append(label or "photo")
            continue
        crop, kps = portrait_crop(img, face)
        photo_id = _add_photo(case_id, "real", label or "Real photo", crop, face.embedding, age)
        anchors.append({"crop": crop, "kps": kps, "emb": face.embedding, "age": age, "photo_id": photo_id})

    if not anchors:
        _purge_case(case_id)
        db.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        raise HTTPException(400, "No clear face was found in the uploaded photos. Use front-facing, well-lit photos.")

    # ---- Step 2 + 3: generated looks -> widened search range
    generated, note = _generate_looks(case_id, anchors, days_missing, fields.get("age_at_missing"))
    db.execute("UPDATE cases SET age_backend_note = ? WHERE id = ?", (note, case_id))

    watchlist.rebuild()
    audit(user["username"], "case_registered", case_code, gd=fields["gd_number"], real_photos=len(anchors),
          generated_looks=generated, rejected_photos=len(rejected))
    return {"id": case_id, "case_code": case_code, "real_photos": len(anchors), "generated_looks": generated,
            "rejected_photos": rejected, "note": note}


def _generate_looks(case_id: int, anchors: list[dict], days_missing: float, age_at_missing) -> tuple[int, str | None]:
    rng = random.Random(case_id * 7919)
    fe = engine()
    note = None

    # Base images: the real photos, plus age-progressed versions for long-running cases.
    bases = [dict(a, label="") for a in anchors]
    progressor = variations.age_progressor()
    if days_missing > 180:
        if progressor.available and age_at_missing is not None:
            current_age = float(age_at_missing) + days_missing / 365.25
            for a in anchors:
                from_age = a["age"] if a["age"] is not None else float(age_at_missing)
                for t in variations.target_ages(from_age, current_age):
                    aged = progressor.progress(a["crop"], a["kps"], from_age, t)
                    if aged is not None:
                        bases.append(dict(a, crop=aged, label=f"age ~{t:.1f}", aged=True))
        else:
            note = ("Child has been missing for more than 6 months, but no age-progression model is enabled. "
                    "Generated looks cover disguise, living-condition and camera changes only.")

    # Most recent photo first: it is the best predictor of the current look.
    bases.sort(key=lambda b: (not b.get("aged"), -(b["age"] or 0)))
    recipes = variations.plan_recipes(days_missing, MAX_GENERATED_LOOKS * 2, rng)

    created = 0
    for j, recipe in enumerate(recipes):
        if created >= MAX_GENERATED_LOOKS:
            break
        base = bases[j % len(bases)]
        img = variations.apply_recipe(base["crop"], base["kps"], recipe, rng)
        face = fe.largest_face(img)
        if face is None:
            continue
        # Identity check: a generated look must still resemble the real child.
        if float(np.dot(face.embedding, base["emb"])) < MIN_VARIANT_SELF_SIM:
            continue
        parts = ([base["label"]] if base["label"] else []) + recipe
        _add_photo(case_id, "predicted", "PREDICTED · " + " + ".join(parts), img, face.embedding)
        created += 1
    return created, note


def _purge_case(case_id: int):
    for p in db.query("SELECT blob FROM photos WHERE case_id = ?", (case_id,)):
        delete_blob(p["blob"])
    for a in db.query("SELECT snapshot_blob, face_blob FROM alerts WHERE case_id = ?", (case_id,)):
        delete_blob(a["snapshot_blob"])
        delete_blob(a["face_blob"])
    db.execute("DELETE FROM signatures WHERE case_id = ?", (case_id,))
    db.execute("DELETE FROM photos WHERE case_id = ?", (case_id,))
    db.execute("UPDATE alerts SET snapshot_blob = NULL, face_blob = NULL, matched_photo_id = NULL WHERE case_id = ?",
               (case_id,))


def close_case(case_id: int, outcome: str, user: dict) -> dict:
    """Automatic case closure: all of the child's photos, predicted looks, signatures and snapshots are
    permanently deleted and personal details are redacted. Only anonymous statistics remain."""
    case = db.one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(404, "Case not found")
    if case["status"] != "active":
        raise HTTPException(400, "Case is already closed")
    if outcome not in ("found", "withdrawn"):
        raise HTTPException(400, "outcome must be 'found' or 'withdrawn'")
    _purge_case(case_id)
    db.execute(
        """UPDATE cases SET status = ?, closed_at = ?, child_name = NULL, guardian_name = NULL,
           guardian_phone = NULL, last_seen_location = NULL, notes = NULL, case_officer_phone = NULL
           WHERE id = ?""",
        (outcome, db.now(), case_id),
    )
    watchlist.rebuild()
    audit(user["username"], "case_closed", case["case_code"], outcome=outcome, data_deleted=True)
    return {"ok": True, "case_code": case["case_code"], "status": outcome}
