"""Face detection (RetinaFace) + face signatures (ArcFace) via InsightFace, with a quality gate."""
import threading
import warnings
from dataclasses import dataclass

import cv2
import numpy as np

from .config import DET_SIZE, FACE_MODEL, MIN_DET_SCORE, MIN_FACE_PX, MIN_QUALITY

warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")


@dataclass
class DetectedFace:
    bbox: np.ndarray        # x1, y1, x2, y2
    kps: np.ndarray         # 5 landmarks: left eye, right eye, nose, mouth left, mouth right
    det_score: float
    embedding: np.ndarray   # L2-normalised 512-d ArcFace signature
    quality: float

    @property
    def width(self) -> float:
        return float(self.bbox[2] - self.bbox[0])


class FaceEngine:
    def __init__(self):
        from insightface.app import FaceAnalysis

        self._app = FaceAnalysis(
            name=FACE_MODEL,
            allowed_modules=["detection", "recognition"],
            providers=["CPUExecutionProvider"],
        )
        self._app.prepare(ctx_id=-1, det_size=(DET_SIZE, DET_SIZE))
        self._lock = threading.Lock()  # ONNX sessions are shared; serialise access

    def detect(self, img_bgr: np.ndarray) -> list[DetectedFace]:
        with self._lock:
            raw = self._app.get(img_bgr)
        faces = []
        for f in raw:
            faces.append(
                DetectedFace(
                    bbox=f.bbox.astype(np.float32),
                    kps=f.kps.astype(np.float32),
                    det_score=float(f.det_score),
                    embedding=f.normed_embedding.astype(np.float32),
                    quality=face_quality(img_bgr, f.bbox, float(f.det_score)),
                )
            )
        return faces

    def largest_face(self, img_bgr: np.ndarray) -> DetectedFace | None:
        faces = self.detect(img_bgr)
        return max(faces, key=lambda f: f.width) if faces else None


def face_quality(img: np.ndarray, bbox, det_score: float) -> float:
    """0..1 score combining detector confidence, face size and sharpness."""
    x1, y1, x2, y2 = [int(v) for v in bbox]
    h, w = img.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0.0
    crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    sharpness = cv2.Laplacian(crop, cv2.CV_64F).var()
    size_f = min(1.0, (x2 - x1) / 80.0)
    sharp_f = min(1.0, sharpness / 60.0)
    return round(det_score * (0.5 + 0.5 * size_f) * (0.6 + 0.4 * sharp_f), 3)


def passes_quality_gate(face: DetectedFace) -> bool:
    return face.width >= MIN_FACE_PX and face.det_score >= MIN_DET_SCORE and face.quality >= MIN_QUALITY


def decode_image(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    # Keep memory bounded for huge phone photos
    h, w = img.shape[:2]
    scale = 1600 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def encode_jpeg(img: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("Could not encode image")
    return buf.tobytes()


def portrait_crop(img: np.ndarray, face: DetectedFace, margin: float = 0.6, size: int = 320):
    """Square crop centred on the face; returns (crop, landmarks in crop coordinates)."""
    x1, y1, x2, y2 = face.bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * (0.5 + margin)
    cy -= (y2 - y1) * 0.08  # leave room above the head for caps / scarves
    src = np.float32([[cx - half, cy - half], [cx + half, cy - half], [cx - half, cy + half]])
    dst = np.float32([[0, 0], [size, 0], [0, size]])
    m = cv2.getAffineTransform(src, dst)
    crop = cv2.warpAffine(img, m, (size, size), borderMode=cv2.BORDER_REPLICATE)
    kps = cv2.transform(face.kps.reshape(-1, 1, 2), m).reshape(-1, 2)
    return crop, kps


_engine: FaceEngine | None = None
_engine_lock = threading.Lock()


def engine() -> FaceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FaceEngine()
        return _engine
