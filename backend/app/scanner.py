"""Step 4 — real-time matching on camera footage.

For every sampled frame: detect faces -> quality gate -> signature -> search the watchlist.
Non-matching faces are dropped immediately (only a counter is incremented). A possible match must be
seen in several frames (or once with a very high score) before an alert is raised for human review.
"""
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from . import db
from .config import (ALERT_COOLDOWN_SEC, HIT_WINDOW_SEC, MATCH_THRESHOLD, MIN_HITS_FOR_ALERT, SCAN_FPS,
                     STRONG_THRESHOLD)
from .face_engine import encode_jpeg, engine, passes_quality_gate
from .security import delete_blob, store_blob
from .watchlist import watchlist


class MatchTracker:
    """Multi-frame confirmation and alert de-duplication for one camera / one scan."""

    def __init__(self, camera_id: int, job_id: int | None, base_time: datetime, vehicle_plate=None, direction=None):
        self.camera_id, self.job_id, self.base_time = camera_id, job_id, base_time
        self.vehicle_plate, self.direction = vehicle_plate, direction
        self.state: dict[int, dict] = {}
        self.alerts_created = 0

    def hit(self, match, t: float, frame: np.ndarray, face):
        st = self.state.get(match.case_id)
        if st is None or (st["alert_id"] and t - st["last_t"] > ALERT_COOLDOWN_SEC) \
                or (not st["alert_id"] and t - st["last_t"] > HIT_WINDOW_SEC):
            st = self.state[match.case_id] = {"hits": deque(), "best": None, "alert_id": None, "last_t": t, "count": 0}
        st["hits"].append(t)
        while st["hits"] and st["hits"][0] < t - HIT_WINDOW_SEC:
            st["hits"].popleft()
        st["last_t"] = t
        st["count"] += 1

        improved = st["best"] is None or match.score > st["best"]["score"]
        if improved:
            st["best"] = {"score": match.score, "real": match.real_score, "photo_id": match.photo_id, "t": t,
                          "snapshot": _snapshot(frame, face), "face": _face_crop(frame, face)}

        if st["alert_id"]:  # already alerted: keep the alert up to date instead of creating duplicates
            b = st["best"]
            if improved:
                old = db.one("SELECT snapshot_blob, face_blob FROM alerts WHERE id = ?", (st["alert_id"],))
                delete_blob(old["snapshot_blob"])
                delete_blob(old["face_blob"])
                db.execute(
                    "UPDATE alerts SET score=?, real_score=?, matched_photo_id=?, snapshot_blob=?, face_blob=?, "
                    "hits=? WHERE id=?",
                    (b["score"], b["real"], b["photo_id"], store_blob(b["snapshot"]), store_blob(b["face"]),
                     st["count"], st["alert_id"]),
                )
            else:
                db.execute("UPDATE alerts SET hits=? WHERE id=?", (st["count"], st["alert_id"]))
            return

        if len(st["hits"]) >= MIN_HITS_FOR_ALERT or match.score >= STRONG_THRESHOLD:
            b = st["best"]
            seen_at = (self.base_time + timedelta(seconds=b["t"])).isoformat(timespec="seconds")
            st["alert_id"] = db.execute(
                """INSERT INTO alerts (case_id, camera_id, job_id, score, real_score, hits, matched_photo_id,
                   snapshot_blob, face_blob, seen_at, video_time, vehicle_plate, direction, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (match.case_id, self.camera_id, self.job_id, b["score"], b["real"], st["count"], b["photo_id"],
                 store_blob(b["snapshot"]), store_blob(b["face"]), seen_at, round(b["t"], 2),
                 self.vehicle_plate, self.direction, db.now()),
            )
            st["best"] = {k: v for k, v in b.items() if k not in ("snapshot", "face")} | {"snapshot": None, "face": None}
            self.alerts_created += 1
            db.bump("alerts_raised")


def _snapshot(frame, face) -> bytes:
    img = frame.copy()
    x1, y1, x2, y2 = [int(v) for v in face.bbox]
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), max(2, img.shape[1] // 400))
    scale = 960 / img.shape[1]
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return encode_jpeg(img, 85)


def _face_crop(frame, face) -> bytes:
    x1, y1, x2, y2 = face.bbox
    m = 0.4 * (x2 - x1)
    h, w = frame.shape[:2]
    crop = frame[max(0, int(y1 - m)):min(h, int(y2 + m)), max(0, int(x1 - m)):min(w, int(x2 + m))]
    return encode_jpeg(cv2.resize(crop, (240, int(240 * crop.shape[0] / max(crop.shape[1], 1)))), 90)


def process_frame(frame: np.ndarray, t: float, tracker: MatchTracker) -> tuple[list[dict], int, int]:
    """Returns (detections for display, faces seen, faces discarded). Nothing about non-matches is stored."""
    detections, seen, discarded = [], 0, 0
    for face in engine().detect(frame):
        seen += 1
        box = [round(float(v), 1) for v in face.bbox]
        if not passes_quality_gate(face):
            discarded += 1
            detections.append({"box": box, "status": "low_quality"})
            continue
        matches = watchlist.search(face.embedding)
        top = matches[0] if matches and matches[0].score >= MATCH_THRESHOLD else None
        if top is None:
            discarded += 1  # the signature goes out of scope here and is never written anywhere
            detections.append({"box": box, "status": "no_match"})
            continue
        tracker.hit(top, t, frame, face)
        detections.append({"box": box, "status": "possible_match", "case_id": top.case_id,
                           "score": round(top.score, 3)})
    return detections, seen, discarded


# ------------------------------------------------------------------ recorded video / stream jobs
_stop_flags: dict[int, threading.Event] = {}


def start_job(job_id: int):
    _stop_flags[job_id] = threading.Event()
    threading.Thread(target=_run_job, args=(job_id,), daemon=True, name=f"scan-{job_id}").start()


def stop_job(job_id: int):
    if job_id in _stop_flags:
        _stop_flags[job_id].set()


def _run_job(job_id: int):
    job = db.one("SELECT * FROM scan_jobs WHERE id = ?", (job_id,))
    source = job["source"]
    is_file = not source.startswith(("rtsp://", "http://", "https://"))
    base_time = datetime.fromisoformat(job["started_at"]) if job["started_at"] else datetime.now(timezone.utc)
    tracker = MatchTracker(job["camera_id"], job_id, base_time, job["vehicle_plate"], job["direction"])
    db.execute("UPDATE scan_jobs SET status='running' WHERE id=?", (job_id,))
    cap = cv2.VideoCapture(source)
    try:
        if not cap.isOpened():
            raise RuntimeError("Could not open video source")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        step = max(1, round(fps / SCAN_FPS))
        idx, frames, seen, discarded = 0, 0, 0, 0
        stop = _stop_flags[job_id]
        while not stop.is_set():
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                t = idx / fps if is_file else time.time() - base_time.timestamp()
                _, s, d = process_frame(frame, t, tracker)
                frames, seen, discarded = frames + 1, seen + s, discarded + d
                db.bump("faces_scanned", s)
                db.bump("faces_discarded", d)
                if frames % 10 == 0:
                    db.execute(
                        "UPDATE scan_jobs SET progress=?, frames_scanned=?, faces_seen=?, faces_discarded=?, "
                        "alerts_created=? WHERE id=?",
                        (min(1.0, idx / total) if total else 0, frames, seen, discarded, tracker.alerts_created, job_id),
                    )
            idx += 1
        db.execute(
            "UPDATE scan_jobs SET status='done', progress=1, frames_scanned=?, faces_seen=?, faces_discarded=?, "
            "alerts_created=?, finished_at=? WHERE id=?",
            (frames, seen, discarded, tracker.alerts_created, db.now(), job_id),
        )
    except Exception as e:  # noqa: BLE001 - surface any failure on the dashboard
        db.execute("UPDATE scan_jobs SET status='failed', error=?, finished_at=? WHERE id=?", (str(e), db.now(), job_id))
    finally:
        cap.release()
        _stop_flags.pop(job_id, None)
        if is_file:
            Path(source).unlink(missing_ok=True)  # footage of the public is not retained after scanning


# ------------------------------------------------------------------ live frames (browser webcam / edge device)
_live_trackers: dict[int, MatchTracker] = {}
_live_t0 = time.time()


def process_live_frame(camera_id: int, frame: np.ndarray, vehicle_plate=None, direction=None) -> list[dict]:
    tracker = _live_trackers.get(camera_id)
    if tracker is None:
        tracker = _live_trackers[camera_id] = MatchTracker(
            camera_id, None, datetime.fromtimestamp(_live_t0, timezone.utc))
    tracker.vehicle_plate, tracker.direction = vehicle_plate or None, direction or None
    detections, seen, discarded = process_frame(frame, time.time() - _live_t0, tracker)
    db.bump("faces_scanned", seen)
    db.bump("faces_discarded", discarded)
    return detections
