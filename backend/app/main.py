"""Shondhan AI — API server and police dashboard."""
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import cases, db, dispatch, scanner
from .config import FRONTEND_DIR, MATCH_THRESHOLD, STRONG_THRESHOLD, UPLOAD_DIR
from .face_engine import decode_image, engine
from .security import audit, current_user, delete_blob, load_blob, login, logout, require
from .watchlist import watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = FastAPI(title="Shondhan AI", version="0.1.0")


@app.on_event("startup")
def startup():
    db.init()
    engine()  # load models once
    watchlist.rebuild()
    # Jobs interrupted by a restart cannot resume
    db.execute("UPDATE scan_jobs SET status='failed', error='server restarted' WHERE status IN ('queued','running')")


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn, response: Response):
    token, session = login(body.username, body.password)
    response.set_cookie("shondhan_session", token, httponly=True, samesite="strict", max_age=12 * 3600)
    return session


@app.post("/api/logout")
def api_logout(response: Response, shondhan_session: str | None = Cookie(default=None)):
    logout(shondhan_session)
    response.delete_cookie("shondhan_session")
    return {"ok": True}


@app.get("/api/me")
def api_me(user=Depends(current_user)):
    return user


# ------------------------------------------------------------------ overview
@app.get("/api/stats")
def api_stats(user=Depends(current_user)):
    reviewed = db.one("SELECT SUM(status='confirmed') AS c, SUM(status='rejected') AS r FROM alerts")
    c, r = reviewed["c"] or 0, reviewed["r"] or 0
    first_alert = db.query(
        """SELECT (julianday(MIN(a.created_at)) - julianday(c.created_at)) * 24 * 60 AS minutes
           FROM alerts a JOIN cases c ON c.id = a.case_id GROUP BY a.case_id"""
    )
    mins = sorted(x["minutes"] for x in first_alert if x["minutes"] is not None)
    return {
        "active_cases": db.one("SELECT COUNT(*) n FROM cases WHERE status='active'")["n"],
        "found_cases": db.one("SELECT COUNT(*) n FROM cases WHERE status='found'")["n"],
        "watchlist_signatures": watchlist.size,
        "cameras": db.one("SELECT COUNT(*) n FROM cameras")["n"],
        "faces_scanned": db.counter("faces_scanned"),
        "faces_discarded": db.counter("faces_discarded"),
        "alerts_pending": db.one("SELECT COUNT(*) n FROM alerts WHERE status='pending'")["n"],
        "alerts_confirmed": c,
        "alerts_rejected": r,
        "false_alert_rate": round(r / (c + r), 3) if c + r else None,
        "median_minutes_to_first_alert": round(mins[len(mins) // 2], 1) if mins else None,
        "thresholds": {"match": MATCH_THRESHOLD, "strong": STRONG_THRESHOLD},
    }


@app.get("/api/cameras")
def api_cameras(user=Depends(current_user)):
    return db.query("SELECT * FROM cameras ORDER BY id")


# ------------------------------------------------------------------ cases
@app.get("/api/cases")
def api_cases(user=Depends(require("case:view"))):
    return db.query(
        """SELECT c.id, c.case_code, c.child_name, c.gender, c.age_at_missing, c.missing_since, c.police_station,
                  c.status, c.created_at, c.closed_at,
                  (SELECT COUNT(*) FROM photos p WHERE p.case_id=c.id AND p.kind='real') AS real_photos,
                  (SELECT COUNT(*) FROM photos p WHERE p.case_id=c.id AND p.kind='predicted') AS predicted_looks,
                  (SELECT COUNT(*) FROM alerts a WHERE a.case_id=c.id AND a.status='confirmed') AS sightings,
                  (SELECT id FROM photos p WHERE p.case_id=c.id AND p.kind='real' ORDER BY id LIMIT 1) AS cover_photo
           FROM cases c ORDER BY c.status='active' DESC, c.id DESC"""
    )


@app.post("/api/cases")
async def api_register_case(
    child_name: str = Form(""),
    gender: str = Form(""),
    age_at_missing: float | None = Form(None),
    missing_since: str = Form(...),
    last_seen_location: str = Form(""),
    gd_number: str = Form(...),
    police_station: str = Form(...),
    guardian_name: str = Form(""),
    guardian_phone: str = Form(""),
    guardian_consent: bool = Form(False),
    case_officer: str = Form(""),
    case_officer_phone: str = Form(""),
    notes: str = Form(""),
    photos: list[UploadFile] = File(...),
    photo_ages: list[str] = Form([]),
    user=Depends(require("case:create")),
):
    items = []
    for i, f in enumerate(photos):
        age_raw = photo_ages[i] if i < len(photo_ages) else ""
        age = float(age_raw) if age_raw.strip() else None
        label = f"Real photo · age {age:g}" if age is not None else "Real photo"
        items.append((await f.read(), age, label))
    fields = dict(child_name=child_name, gender=gender, age_at_missing=age_at_missing, missing_since=missing_since,
                  last_seen_location=last_seen_location, gd_number=gd_number, police_station=police_station,
                  guardian_name=guardian_name, guardian_phone=guardian_phone, guardian_consent=guardian_consent,
                  case_officer=case_officer, case_officer_phone=case_officer_phone, notes=notes)
    return await run_in_threadpool(cases.register_case, fields, items, user)


@app.get("/api/cases/{case_id}")
def api_case(case_id: int, user=Depends(require("case:view"))):
    case = db.one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(404, "Case not found")
    case["photos"] = db.query("SELECT id, kind, label, age_at_photo FROM photos WHERE case_id = ? ORDER BY kind DESC, id",
                              (case_id,))
    case["alerts"] = db.query(
        """SELECT a.id, a.status, a.score, a.seen_at, a.vehicle_plate, k.name AS camera_name
           FROM alerts a JOIN cameras k ON k.id = a.camera_id WHERE a.case_id = ? ORDER BY a.seen_at DESC""",
        (case_id,),
    )
    audit(user["username"], "case_viewed", case["case_code"])
    return case


class CloseIn(BaseModel):
    outcome: str


@app.post("/api/cases/{case_id}/close")
def api_close_case(case_id: int, body: CloseIn, user=Depends(require("case:close"))):
    return cases.close_case(case_id, body.outcome, user)


@app.get("/api/cases/{case_id}/route")
def api_case_route(case_id: int, user=Depends(require("case:view"))):
    return dispatch.case_route(case_id)


@app.get("/api/photos/{photo_id}/image")
def api_photo(photo_id: int, user=Depends(require("case:view"))):
    p = db.one("SELECT blob FROM photos WHERE id = ?", (photo_id,))
    if not p:
        raise HTTPException(404, "Photo not found")
    return Response(load_blob(p["blob"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ alerts (human verification)
ALERT_SQL = """SELECT a.*, c.case_code, c.child_name, c.age_at_missing, c.missing_since, c.status AS case_status,
                      k.name AS camera_name, k.type AS camera_type, k.lat, k.lon, k.district, k.nearest_station,
                      p.label AS matched_label, p.kind AS matched_kind
               FROM alerts a JOIN cases c ON c.id = a.case_id JOIN cameras k ON k.id = a.camera_id
               LEFT JOIN photos p ON p.id = a.matched_photo_id"""


@app.get("/api/alerts")
def api_alerts(status: str | None = None, user=Depends(require("case:view"))):
    if status:
        return db.query(ALERT_SQL + " WHERE a.status = ? ORDER BY a.created_at DESC", (status,))
    return db.query(ALERT_SQL + " ORDER BY a.status='pending' DESC, a.created_at DESC LIMIT 200")


@app.get("/api/alerts/{alert_id}")
def api_alert(alert_id: int, user=Depends(require("case:view"))):
    a = db.one(ALERT_SQL + " WHERE a.id = ?", (alert_id,))
    if not a:
        raise HTTPException(404, "Alert not found")
    a["real_photos"] = [p["id"] for p in db.query("SELECT id FROM photos WHERE case_id=? AND kind='real'", (a["case_id"],))]
    a["notifications"] = db.query("SELECT * FROM notifications WHERE alert_id = ?", (alert_id,))
    return a


@app.get("/api/alerts/{alert_id}/{which}")
def api_alert_image(alert_id: int, which: str, user=Depends(require("case:view"))):
    if which not in ("snapshot", "face"):
        raise HTTPException(404)
    a = db.one(f"SELECT {which}_blob AS blob FROM alerts WHERE id = ?", (alert_id,))
    if not a or not a["blob"]:
        raise HTTPException(404, "Image not available")
    return Response(load_blob(a["blob"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


class ReviewIn(BaseModel):
    decision: str  # confirm | reject
    note: str = ""


@app.post("/api/alerts/{alert_id}/review")
def api_review(alert_id: int, body: ReviewIn, user=Depends(require("alert:review"))):
    a = db.one("SELECT a.*, c.case_code FROM alerts a JOIN cases c ON c.id=a.case_id WHERE a.id = ?", (alert_id,))
    if not a:
        raise HTTPException(404, "Alert not found")
    if a["status"] != "pending":
        raise HTTPException(400, f"Alert already {a['status']}")
    if body.decision not in ("confirm", "reject"):
        raise HTTPException(400, "decision must be 'confirm' or 'reject'")
    status = "confirmed" if body.decision == "confirm" else "rejected"
    db.execute("UPDATE alerts SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
               (status, user["username"], db.now(), body.note, alert_id))
    sent = []
    if status == "confirmed":
        sent = dispatch.dispatch_confirmed_alert(alert_id)
    else:
        # A rejected match is someone else's face: delete the snapshot now.
        delete_blob(a["snapshot_blob"])
        delete_blob(a["face_blob"])
        db.execute("UPDATE alerts SET snapshot_blob=NULL, face_blob=NULL WHERE id=?", (alert_id,))
    audit(user["username"], f"alert_{status}", f"alert #{alert_id}", case=a["case_code"], score=round(a["score"], 3),
          note=body.note or None)
    return {"status": status, "notified": sent}


@app.get("/api/notifications")
def api_notifications(user=Depends(require("case:view"))):
    return db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 100")


# ------------------------------------------------------------------ scanning
@app.post("/api/scans")
async def api_start_scan(
    camera_id: int = Form(...),
    video: UploadFile | None = File(None),
    stream_url: str = Form(""),
    vehicle_plate: str = Form(""),
    direction: str = Form(""),
    recorded_at: str = Form(""),
    user=Depends(require("scan:run")),
):
    if not db.one("SELECT id FROM cameras WHERE id = ?", (camera_id,)):
        raise HTTPException(400, "Unknown camera")
    if video is not None and video.filename:
        path = UPLOAD_DIR / f"{uuid.uuid4().hex}{Path(video.filename).suffix.lower() or '.mp4'}"
        with open(path, "wb") as out:
            while chunk := await video.read(1 << 20):
                out.write(chunk)
        source = str(path)
    elif stream_url.startswith(("rtsp://", "http://", "https://")):
        source = stream_url
    else:
        raise HTTPException(400, "Upload a video file or give an RTSP/HTTP stream URL")
    try:
        started = datetime.fromisoformat(recorded_at) if recorded_at else datetime.now(timezone.utc)
    except ValueError:
        raise HTTPException(400, "recorded_at must be an ISO date-time")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    started = started.astimezone(timezone.utc)  # all stored times are UTC so sightings sort correctly
    job_id = db.execute(
        "INSERT INTO scan_jobs (camera_id, source, vehicle_plate, direction, status, started_at, created_by) "
        "VALUES (?,?,?,?,?,?,?)",
        (camera_id, source, vehicle_plate or None, direction or None, "queued", started.isoformat(timespec="seconds"),
         user["username"]),
    )
    audit(user["username"], "scan_started", f"job #{job_id}", camera_id=camera_id,
          source="stream" if source == stream_url else "video upload")
    scanner.start_job(job_id)
    return {"id": job_id}


@app.get("/api/scans")
def api_scans(user=Depends(require("scan:run"))):
    return db.query(
        """SELECT j.id, j.camera_id, k.name AS camera_name, j.status, j.progress, j.frames_scanned, j.faces_seen,
                  j.faces_discarded, j.alerts_created, j.error, j.started_at, j.finished_at, j.vehicle_plate,
                  CASE WHEN j.source LIKE 'rtsp://%' OR j.source LIKE 'http%' THEN 'stream' ELSE 'video' END AS kind
           FROM scan_jobs j JOIN cameras k ON k.id = j.camera_id ORDER BY j.id DESC LIMIT 50"""
    )


@app.post("/api/scans/{job_id}/stop")
def api_stop_scan(job_id: int, user=Depends(require("scan:run"))):
    scanner.stop_job(job_id)
    return {"ok": True}


@app.post("/api/live/frame")
async def api_live_frame(
    camera_id: int = Form(...),
    frame: UploadFile = File(...),
    vehicle_plate: str = Form(""),
    direction: str = Form(""),
    user=Depends(require("scan:run")),
):
    img = decode_image(await frame.read())
    if img is None:
        raise HTTPException(400, "Bad frame")
    dets = await run_in_threadpool(scanner.process_live_frame, camera_id, img, vehicle_plate, direction)
    codes = {c["id"]: c["case_code"] for c in db.query("SELECT id, case_code FROM cases WHERE status='active'")}
    for d in dets:
        if "case_id" in d:
            d["case_code"] = codes.get(d["case_id"])
    return {"width": img.shape[1], "height": img.shape[0], "detections": dets}


# ------------------------------------------------------------------ audit
@app.get("/api/audit")
def api_audit(user=Depends(require("audit:view"))):
    return db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 300")


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
