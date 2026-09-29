"""Step 2 — AI appearance generation ("up to 50 possible looks").

Two kinds of generators:

1. Age progression (how the child may look after 6 months, 1, 3, 5 years). This needs a generative
   model (GAN / diffusion face ageing) and a GPU. It is a pluggable backend: see `AgeProgressor`.
   The MVP ships with no model enabled, and says so on every case, rather than faking ageing.

2. Disguise, living-condition and camera-condition edits, done with landmark-guided image editing
   (runs on any laptop CPU): cap, scarf/hijab, glasses, sunglasses, sun-tan, tired/pale face,
   dirt, weight loss, CCTV low resolution, motion blur, night-IR, low light, compression, head tilt.

The case timeline decides the mix: a recent case focuses on disguises; a case missing for years
focuses on age progression.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import AGE_BACKEND


@dataclass
class Variant:
    img: np.ndarray
    label: str
    recipe: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ geometry helpers
def _geom(kps: np.ndarray):
    le, re, nose, ml, mr = kps
    eye_c = (le + re) / 2
    mouth_c = (ml + mr) / 2
    ed = float(np.linalg.norm(re - le)) or 30.0
    return le, re, nose, eye_c, mouth_c, ed


def _blend(img, overlay, mask):
    mask = mask.astype(np.float32)[..., None]
    return (img.astype(np.float32) * (1 - mask) + overlay.astype(np.float32) * mask).clip(0, 255).astype(np.uint8)


def _face_mask(img, kps, soft=True):
    _, _, _, eye_c, mouth_c, ed = _geom(kps)
    mask = np.zeros(img.shape[:2], np.float32)
    center = (int(eye_c[0]), int((eye_c[1] + mouth_c[1]) / 2))
    cv2.ellipse(mask, center, (int(1.15 * ed), int(1.6 * ed)), 0, 0, 360, 1.0, -1)
    if soft:
        mask = cv2.GaussianBlur(mask, (0, 0), ed * 0.25)
    return mask


# ------------------------------------------------------------------ living-condition edits
def sun_tan(img, kps, rng):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] -= rng.uniform(12, 26)
    lab[..., 1] += rng.uniform(2, 5)
    lab[..., 2] += rng.uniform(6, 14)
    toned = cv2.cvtColor(lab.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    return _blend(img, toned, _face_mask(img, kps) * 0.9 + 0.1)


def tired_pale(img, kps, rng):
    le, re, _, _, _, ed = _geom(kps)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] *= rng.uniform(0.5, 0.7)
    out = cv2.cvtColor(hsv.clip(0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
    shadow = np.zeros(img.shape[:2], np.float32)
    for eye in (le, re):  # dark circles under the eyes
        cv2.ellipse(shadow, (int(eye[0]), int(eye[1] + 0.28 * ed)), (int(0.3 * ed), int(0.12 * ed)), 0, 0, 360, 1.0, -1)
    shadow = cv2.GaussianBlur(shadow, (0, 0), ed * 0.08) * rng.uniform(0.25, 0.4)
    return (out.astype(np.float32) * (1 - shadow[..., None])).clip(0, 255).astype(np.uint8)


def dirt(img, kps, rng):
    h, w = img.shape[:2]
    noise = rng.normal(0.5, 0.2, (h // 16 + 1, w // 16 + 1)).astype(np.float32)
    noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_CUBIC)
    blotches = np.clip((noise - 0.55) * 3, 0, 1) * rng.uniform(0.25, 0.4)
    brown = np.full_like(img, (45, 65, 90))
    return _blend(img, brown, blotches * _face_mask(img, kps))


def thinner_face(img, kps, rng):
    """Weight loss: pull the cheeks and jaw inwards with a smooth warp."""
    _, _, _, eye_c, mouth_c, ed = _geom(kps)
    h, w = img.shape[:2]
    s = rng.uniform(0.10, 0.18)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    chin_y = mouth_c[1] + 0.9 * ed
    wy = np.clip((ys - eye_c[1]) / max(chin_y - eye_c[1], 1), 0, 1)
    wy = np.where(ys > chin_y, np.clip(1 - (ys - chin_y) / ed, 0, 1), wy)
    wx = np.exp(-(((xs - eye_c[0]) / (1.6 * ed)) ** 2))
    map_x = eye_c[0] + (xs - eye_c[0]) * (1 + s * wy * wx)
    return cv2.remap(img, map_x, ys, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# ------------------------------------------------------------------ disguise edits
_CLOTH = [(40, 40, 40), (30, 30, 120), (120, 60, 20), (200, 200, 200), (20, 90, 30), (90, 30, 90), (60, 110, 160)]


def _texture(img, color, rng):
    layer = np.full_like(img, color).astype(np.float32)
    noise = rng.normal(0, 8, img.shape[:2]).astype(np.float32)
    return (layer + noise[..., None]).clip(0, 255).astype(np.uint8)


def cap(img, kps, rng):
    _, _, _, eye_c, _, ed = _geom(kps)
    mask = np.zeros(img.shape[:2], np.float32)
    brow_y = eye_c[1] - 0.45 * ed
    cv2.ellipse(mask, (int(eye_c[0]), int(brow_y)), (int(1.45 * ed), int(1.5 * ed)), 0, 180, 360, 1.0, -1)
    cv2.ellipse(mask, (int(eye_c[0]), int(brow_y)), (int(1.55 * ed), int(0.22 * ed)), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.5)
    return _blend(img, _texture(img, rng.choice(_CLOTH), rng), mask)


def scarf(img, kps, rng):
    """Head scarf / hijab: cloth around the face, face oval left visible."""
    _, _, _, eye_c, mouth_c, ed = _geom(kps)
    h, w = img.shape[:2]
    outer = np.zeros((h, w), np.float32)
    cv2.ellipse(outer, (int(eye_c[0]), int(eye_c[1] - 0.2 * ed)), (int(1.75 * ed), int(2.0 * ed)), 0, 0, 360, 1.0, -1)
    x0, x1 = int(eye_c[0] - 1.8 * ed), int(eye_c[0] + 1.8 * ed)
    outer[int(eye_c[1]):, max(0, x0):min(w, x1)] = 1.0
    inner = np.zeros((h, w), np.float32)
    cy = (eye_c[1] - 0.75 * ed + mouth_c[1] + 0.8 * ed) / 2
    ry = (mouth_c[1] + 0.8 * ed - (eye_c[1] - 0.75 * ed)) / 2
    cv2.ellipse(inner, (int(eye_c[0]), int(cy)), (int(1.08 * ed), int(ry)), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(np.clip(outer - inner, 0, 1), (0, 0), 2)
    return _blend(img, _texture(img, rng.choice(_CLOTH), rng), mask)


def glasses(img, kps, rng, dark=False):
    le, re, _, _, _, ed = _geom(kps)
    out = img.copy()
    frame = (20, 20, 20) if rng.random() < 0.7 else (40, 60, 120)
    t = max(2, int(ed * 0.06))
    axes = (int(0.36 * ed), int(0.26 * ed))
    if dark:
        lens = np.zeros(img.shape[:2], np.float32)
        for eye in (le, re):
            cv2.ellipse(lens, (int(eye[0]), int(eye[1])), axes, 0, 0, 360, 0.88, -1)
        out = _blend(out, np.full_like(img, (15, 15, 15)), cv2.GaussianBlur(lens, (0, 0), 1))
    for eye in (le, re):
        cv2.ellipse(out, (int(eye[0]), int(eye[1])), axes, 0, 0, 360, frame, t, cv2.LINE_AA)
    cv2.line(out, (int(le[0] + axes[0]), int(le[1])), (int(re[0] - axes[0]), int(re[1])), frame, t, cv2.LINE_AA)
    cv2.line(out, (int(le[0] - axes[0]), int(le[1])), (int(le[0] - 0.95 * ed), int(le[1] - 0.05 * ed)), frame, t, cv2.LINE_AA)
    cv2.line(out, (int(re[0] + axes[0]), int(re[1])), (int(re[0] + 0.95 * ed), int(re[1] - 0.05 * ed)), frame, t, cv2.LINE_AA)
    return out


def sunglasses(img, kps, rng):
    return glasses(img, kps, rng, dark=True)


# ------------------------------------------------------------------ camera-condition edits
def cctv_lowres(img, kps, rng):
    _, _, _, _, _, ed = _geom(kps)
    h, w = img.shape[:2]
    scale = rng.uniform(14, 22) / ed  # ~ 35-55 px face width, typical for a toll plaza camera
    small = cv2.resize(img, (max(8, int(w * scale)), max(8, int(h * scale))), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def motion_blur(img, kps, rng):
    k = int(rng.choice([5, 7, 9]))
    kernel = np.zeros((k, k), np.float32)
    kernel[k // 2, :] = 1.0 / k
    return cv2.filter2D(img, -1, kernel)


def night_ir(img, kps, rng):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.equalizeHist(g).astype(np.float32) + rng.normal(0, 7, g.shape)
    return cv2.cvtColor(g.clip(0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def low_light(img, kps, rng):
    f = (img.astype(np.float32) / 255.0) ** rng.uniform(1.6, 2.2) * 255
    f += rng.normal(0, 6, img.shape)
    return f.clip(0, 255).astype(np.uint8)


def compression(img, kps, rng):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.uniform(12, 25))])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def head_tilt(img, kps, rng):
    _, _, _, eye_c, _, _ = _geom(kps)
    angle = rng.choice([-1, 1]) * rng.uniform(6, 12)
    m = cv2.getRotationMatrix2D((float(eye_c[0]), float(eye_c[1])), angle, 1.0)
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), borderMode=cv2.BORDER_REPLICATE)


def mirror(img, kps, rng):
    return cv2.flip(img, 1)


DISGUISE = {"cap": cap, "head scarf / hijab": scarf, "glasses": glasses, "sunglasses": sunglasses}
CONDITION = {"sun-tanned": sun_tan, "tired / pale": tired_pale, "dirty face": dirt, "weight loss": thinner_face}
CAMERA = {
    "CCTV low-res": cctv_lowres, "motion blur": motion_blur, "night IR": night_ir,
    "low light": low_light, "heavy compression": compression,
}
POSE = {"head tilt": head_tilt, "mirrored": mirror}
# Edits that change landmark positions go last so earlier edits can use the original landmarks.
_ORDER = list(CONDITION) + list(DISGUISE) + list(POSE) + list(CAMERA)
_ALL = {**CONDITION, **DISGUISE, **POSE, **CAMERA}


# ------------------------------------------------------------------ age progression (pluggable)
class AgeProgressor:
    """Interface for a generative face-ageing model.

    Implement `progress` with a GAN / diffusion ageing model (e.g. a SAM-style style-based
    regression model, or an identity-preserving diffusion pipeline) running on a local GPU.
    Children's photos must not be sent to third-party APIs.
    """

    name = "none"
    available = False

    def progress(self, img: np.ndarray, kps: np.ndarray, from_age: float, to_age: float) -> np.ndarray | None:
        return None


AGE_BACKENDS: dict[str, type[AgeProgressor]] = {"none": AgeProgressor}


def age_progressor() -> AgeProgressor:
    return AGE_BACKENDS.get(AGE_BACKEND, AgeProgressor)()


# ------------------------------------------------------------------ planning
def plan_recipes(days_missing: float, n: int, rng: random.Random) -> list[list[str]]:
    """Choose which edits to combine. Recent cases -> disguises; old cases -> conditions + camera."""
    recent = days_missing <= 180
    p_disguise = 0.75 if recent else 0.4
    p_condition = 0.45 if recent else 0.75
    recipes, seen = [], set()
    # Always include a few pure camera-condition looks: how the *real* photo looks on CCTV.
    for cam in ["CCTV low-res", "night IR", "motion blur"]:
        recipes.append([cam])
        seen.add((cam,))
    attempts = 0
    while len(recipes) < n and attempts < n * 30:
        attempts += 1
        r = []
        if rng.random() < p_condition:
            r += rng.sample(list(CONDITION), k=rng.choice([1, 1, 2]))
        if rng.random() < p_disguise:
            r.append(rng.choice(list(DISGUISE)))
        if rng.random() < 0.3:
            r.append(rng.choice(list(POSE)))
        if rng.random() < 0.6:
            r.append(rng.choice(list(CAMERA)))
        if not r:
            continue
        r = sorted(set(r), key=_ORDER.index)
        if "glasses" in r and "sunglasses" in r:
            continue
        key = tuple(r)
        if key not in seen:
            seen.add(key)
            recipes.append(r)
    return recipes[:n]


def apply_recipe(img: np.ndarray, kps: np.ndarray, recipe: list[str], rng: random.Random) -> np.ndarray:
    out = img
    nrng = np.random.default_rng(rng.randrange(1 << 30))

    class _R:  # small adapter: python-random API used by edits, backed by numpy for arrays
        uniform = staticmethod(rng.uniform)
        choice = staticmethod(rng.choice)
        random = staticmethod(rng.random)
        normal = staticmethod(nrng.normal)

    for step in recipe:
        out = _ALL[step](out, kps, _R)
    return out


def target_ages(age_at_photo: float, current_age: float) -> list[float]:
    """Ages to progress to: +6 months, +1, +3, +5 years (as in the proposal), up to now + 2 years."""
    targets = {round(age_at_photo + d, 1) for d in (0.5, 1, 3, 5)} | {round(current_age, 1)}
    return sorted(t for t in targets if age_at_photo < t <= current_age + 2)
