"""Encryption at rest, role-based access and the audit trail."""
import hmac
import json
import secrets
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import Cookie, Depends, HTTPException

from . import db
from .config import BLOB_DIR, KEY_PATH, USERS

# ---------------------------------------------------------------- encryption
# In production the key comes from a KMS/HSM. For the MVP it is generated once and kept in data/.
if not KEY_PATH.exists():
    KEY_PATH.write_bytes(Fernet.generate_key())
    KEY_PATH.chmod(0o600)
_fernet = Fernet(KEY_PATH.read_bytes())


def store_blob(data: bytes) -> str:
    """Encrypt bytes to disk; returns an opaque blob id."""
    blob_id = uuid.uuid4().hex
    (BLOB_DIR / f"{blob_id}.enc").write_bytes(_fernet.encrypt(data))
    return blob_id


def load_blob(blob_id: str) -> bytes:
    path = BLOB_DIR / f"{Path(blob_id).name}.enc"
    if not path.exists():
        raise HTTPException(404, "Image not found (it may have been deleted on case closure)")
    return _fernet.decrypt(path.read_bytes())


def delete_blob(blob_id: str | None):
    if blob_id:
        (BLOB_DIR / f"{Path(blob_id).name}.enc").unlink(missing_ok=True)


# ---------------------------------------------------------------- auth
_sessions: dict[str, dict] = {}

PERMISSIONS = {
    "admin": {"case:create", "case:view", "case:close", "alert:review", "scan:run", "audit:view"},
    "officer": {"case:view", "case:close", "alert:review", "scan:run"},
    "registrar": {"case:create", "case:view"},
}


def login(username: str, password: str) -> tuple[str, dict]:
    user = USERS.get(username)
    if not user or not hmac.compare_digest(user["password"], password):
        audit(username or "?", "login_failed")
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    session = {"username": username, "role": user["role"], "name": user["name"]}
    _sessions[token] = session
    audit(username, "login")
    return token, session


def logout(token: str | None):
    if token:
        _sessions.pop(token, None)


def current_user(shondhan_session: str | None = Cookie(default=None)) -> dict:
    session = _sessions.get(shondhan_session or "")
    if not session:
        raise HTTPException(401, "Not logged in")
    return session


def require(permission: str):
    def checker(user: dict = Depends(current_user)) -> dict:
        if permission not in PERMISSIONS.get(user["role"], set()):
            raise HTTPException(403, f"Your role ({user['role']}) cannot perform '{permission}'")
        return user

    return checker


# ---------------------------------------------------------------- audit
def audit(user: str, action: str, target: str | None = None, **details):
    db.execute(
        "INSERT INTO audit (ts, user, action, target, details) VALUES (?,?,?,?,?)",
        (db.now(), user, action, target, json.dumps(details) if details else None),
    )
