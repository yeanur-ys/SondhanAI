"""Step 5/6 — alert dispatch after human verification, and route prediction from sightings."""
import logging
import math

from . import db

log = logging.getLogger("shondhan.dispatch")

HIGHWAY_TYPES = {"toll_plaza", "highway"}


def send_sms(recipient: str, message: str) -> str:
    """SMS gateway hook. The MVP only records the message; plug in an operator SMS API for the pilot."""
    log.warning("SMS (simulated) -> %s: %s", recipient, message)
    return "sms (simulated)"


def dispatch_confirmed_alert(alert_id: int) -> list[dict]:
    a = db.one(
        """SELECT a.*, c.case_code, c.police_station, c.case_officer, c.case_officer_phone,
                  k.name AS camera_name, k.type AS camera_type, k.nearest_station
           FROM alerts a JOIN cases c ON c.id = a.case_id JOIN cameras k ON k.id = a.camera_id
           WHERE a.id = ?""",
        (alert_id,),
    )
    # Only the case code is sent, never the child's name or photo, to keep SMS content minimal.
    parts = [f"SHONDHAN ALERT {a['case_code']}: verified sighting at {a['camera_name']}, {a['seen_at']} UTC."]
    if a["vehicle_plate"]:
        parts.append(f"Vehicle {a['vehicle_plate']}" + (f" heading {a['direction']}." if a["direction"] else "."))
    parts.append(f"Open alert #{alert_id} on the Shondhan dashboard.")
    message = " ".join(parts)

    recipients = [f"{a['nearest_station']} (nearest station)"]
    if a["camera_type"] in HIGHWAY_TYPES:
        recipients.append("Highway Police control room")
    officer = a["case_officer"] or "Case officer"
    recipients.append(f"{officer}, {a['police_station']}" + (f" ({a['case_officer_phone']})" if a["case_officer_phone"] else ""))

    sent = []
    for r in recipients:
        channel = send_sms(r, message)
        db.execute("INSERT INTO notifications (alert_id, recipient, channel, message, created_at) VALUES (?,?,?,?,?)",
                   (alert_id, r, channel, message, db.now()))
        sent.append({"recipient": r, "channel": channel})
    return sent


# ------------------------------------------------------------------ route prediction
def _haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _bearing(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin(lon2 - lon1) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def case_route(case_id: int) -> dict:
    """Confirmed sightings in time order, plus the checkpoints most likely to be reached next.

    Heuristic for the MVP: continue the direction of travel between the last two distinct sightings and
    rank cameras ahead (within ±60°) by distance. A pilot would use the real road graph and travel times.
    """
    sightings = db.query(
        """SELECT a.id, a.seen_at, a.vehicle_plate, a.direction, a.score, k.id AS camera_id, k.name, k.lat, k.lon
           FROM alerts a JOIN cameras k ON k.id = a.camera_id
           WHERE a.case_id = ? AND a.status = 'confirmed' ORDER BY a.seen_at""",
        (case_id,),
    )
    cameras = db.query("SELECT * FROM cameras")
    if not sightings:
        return {"sightings": [], "next_checkpoints": [], "method": None}

    last = sightings[-1]
    last_pt = (last["lat"], last["lon"])
    prev = next((s for s in reversed(sightings[:-1]) if s["camera_id"] != last["camera_id"]), None)
    candidates = []
    for cam in cameras:
        if cam["id"] == last["camera_id"]:
            continue
        dist = _haversine_km(last_pt, (cam["lat"], cam["lon"]))
        if prev:
            travel = _bearing((prev["lat"], prev["lon"]), last_pt)
            diff = abs((_bearing(last_pt, (cam["lat"], cam["lon"])) - travel + 180) % 360 - 180)
            if diff > 60 or dist > 300:
                continue
        elif dist > 150:
            continue
        candidates.append({"camera_id": cam["id"], "name": cam["name"], "lat": cam["lat"], "lon": cam["lon"],
                           "distance_km": round(dist, 1)})
    candidates.sort(key=lambda c: c["distance_km"])
    method = "direction of travel" if prev else "nearest checkpoints (single sighting)"
    return {"sightings": sightings, "next_checkpoints": candidates[:3], "method": method}
