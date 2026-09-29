"""SQLite storage. Small, dependency-free and good enough for the MVP and a single-site pilot."""
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_code TEXT UNIQUE NOT NULL,
    child_name TEXT,
    gender TEXT,
    age_at_missing REAL,
    missing_since TEXT NOT NULL,
    last_seen_location TEXT,
    gd_number TEXT NOT NULL,
    police_station TEXT NOT NULL,
    guardian_name TEXT,
    guardian_phone TEXT,
    guardian_consent INTEGER NOT NULL,
    case_officer TEXT,
    case_officer_phone TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',      -- active | found | withdrawn
    age_backend_note TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                          -- real | predicted
    label TEXT NOT NULL,
    age_at_photo REAL,
    blob TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    district TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    nearest_station TEXT
);
CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    source TEXT NOT NULL,
    vehicle_plate TEXT,
    direction TEXT,
    status TEXT NOT NULL,                        -- queued | running | done | failed
    progress REAL DEFAULT 0,
    frames_scanned INTEGER DEFAULT 0,
    faces_seen INTEGER DEFAULT 0,
    faces_discarded INTEGER DEFAULT 0,
    alerts_created INTEGER DEFAULT 0,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    job_id INTEGER,
    score REAL NOT NULL,
    real_score REAL,
    hits INTEGER NOT NULL DEFAULT 1,
    matched_photo_id INTEGER,
    snapshot_blob TEXT,
    face_blob TEXT,
    seen_at TEXT NOT NULL,
    video_time REAL,
    vehicle_plate TEXT,
    direction TEXT,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | confirmed | rejected
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

# Approximate public locations used for the demo map. Replace with real camera registry in a pilot.
DEMO_CAMERAS = [
    ("Padma Bridge Toll Plaza (Mawa)", "toll_plaza", "Munshiganj", 23.4436, 90.2610, "Lohajang Police Station"),
    ("Padma Bridge Toll Plaza (Jajira)", "toll_plaza", "Shariatpur", 23.3960, 90.2850, "Jajira Police Station"),
    ("Bangabandhu (Jamuna) Bridge Toll Plaza", "toll_plaza", "Tangail", 24.4010, 89.8020, "Bhuapur Police Station"),
    ("Meghna Bridge Toll Plaza", "toll_plaza", "Munshiganj", 23.6070, 90.6180, "Gazaria Police Station"),
    ("Kamalapur Railway Station", "railway_station", "Dhaka", 23.7317, 90.4259, "Kamalapur Railway Police Station"),
    ("Sayedabad Bus Terminal", "bus_terminal", "Dhaka", 23.7176, 90.4262, "Jatrabari Police Station"),
    ("Gabtoli Bus Terminal", "bus_terminal", "Dhaka", 23.7836, 90.3446, "Darus Salam Police Station"),
    ("Sadarghat Launch Terminal", "launch_ghat", "Dhaka", 23.7055, 90.4081, "Kotwali Police Station"),
    ("Chattogram Railway Station", "railway_station", "Chattogram", 22.3350, 91.8325, "Kotwali Police Station (Ctg)"),
    ("Benapole Land Port", "land_port", "Jashore", 23.0437, 88.8953, "Benapole Port Police Station"),
    ("Dhaka-Chattogram Highway, Daudkandi", "highway", "Cumilla", 23.5260, 90.7160, "Daudkandi Highway Police"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with _lock:
        _conn.executescript(SCHEMA)
        if _conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 0:
            _conn.executemany(
                "INSERT INTO cameras (name, type, district, lat, lon, nearest_station) VALUES (?,?,?,?,?,?)",
                DEMO_CAMERAS,
            )
        _conn.commit()


def execute(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def bump(name: str, by: int = 1):
    """Aggregate counters (e.g. faces scanned and discarded). Only numbers are kept, never faces."""
    if by:
        execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, by),
        )


def counter(name: str) -> int:
    row = one("SELECT value FROM counters WHERE name = ?", (name,))
    return row["value"] if row else 0
