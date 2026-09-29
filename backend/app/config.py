"""Central configuration. Every value can be overridden with an environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("SHONDHAN_DATA_DIR", ROOT / "data"))
BLOB_DIR = DATA_DIR / "blobs"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "shondhan.db"
KEY_PATH = DATA_DIR / "secret.key"
FRONTEND_DIR = ROOT / "frontend"

for d in (DATA_DIR, BLOB_DIR, UPLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Face models (InsightFace "buffalo_l" = RetinaFace detector + ArcFace R50 recogniser) ---
FACE_MODEL = os.getenv("SHONDHAN_FACE_MODEL", "buffalo_l")
DET_SIZE = int(os.getenv("SHONDHAN_DET_SIZE", "640"))

# --- Matching thresholds (cosine similarity of ArcFace embeddings) ---
MATCH_THRESHOLD = float(os.getenv("SHONDHAN_MATCH_THRESHOLD", "0.40"))    # possible match
STRONG_THRESHOLD = float(os.getenv("SHONDHAN_STRONG_THRESHOLD", "0.55"))  # alert from a single frame
GENERATED_PENALTY = float(os.getenv("SHONDHAN_GENERATED_PENALTY", "0.04"))  # real photos weigh more
MIN_HITS_FOR_ALERT = int(os.getenv("SHONDHAN_MIN_HITS", "2"))             # multi-frame confirmation
HIT_WINDOW_SEC = float(os.getenv("SHONDHAN_HIT_WINDOW", "10"))
ALERT_COOLDOWN_SEC = float(os.getenv("SHONDHAN_ALERT_COOLDOWN", "30"))

# --- Face quality gate for camera frames ---
MIN_FACE_PX = int(os.getenv("SHONDHAN_MIN_FACE_PX", "32"))
MIN_DET_SCORE = float(os.getenv("SHONDHAN_MIN_DET_SCORE", "0.5"))
MIN_QUALITY = float(os.getenv("SHONDHAN_MIN_QUALITY", "0.30"))

# --- Appearance generation ---
MAX_GENERATED_LOOKS = int(os.getenv("SHONDHAN_MAX_LOOKS", "50"))
# A generated look must still resemble the real photo, otherwise it is discarded:
# heavy occlusions can collapse into a "generic face" and cause false matches.
MIN_VARIANT_SELF_SIM = float(os.getenv("SHONDHAN_MIN_VARIANT_SIM", "0.35"))
AGE_BACKEND = os.getenv("SHONDHAN_AGE_BACKEND", "none")

# --- Video scanning ---
SCAN_FPS = float(os.getenv("SHONDHAN_SCAN_FPS", "5"))

# --- Demo users (replace with the police identity provider in a pilot) ---
USERS = {
    "admin": {"password": os.getenv("SHONDHAN_ADMIN_PASSWORD", "admin123"), "role": "admin", "name": "System Admin"},
    "officer": {"password": os.getenv("SHONDHAN_OFFICER_PASSWORD", "officer123"), "role": "officer", "name": "Verification Officer"},
    "registrar": {"password": os.getenv("SHONDHAN_REGISTRAR_PASSWORD", "registrar123"), "role": "registrar", "name": "Duty Officer (Case Desk)"},
}
