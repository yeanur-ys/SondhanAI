"""Step 3/4 — the watchlist: face signatures of *registered missing children only*, in FAISS.

Camera faces are compared against this index and nothing else. There is no index of the public.
"""
import threading
from dataclasses import dataclass

import faiss
import numpy as np

from . import db
from .config import GENERATED_PENALTY

DIM = 512


@dataclass
class CaseMatch:
    case_id: int
    score: float          # best adjusted similarity (generated looks are penalised)
    real_score: float     # best similarity against a real photo
    photo_id: int         # the photo / predicted look that matched best
    kind: str


class Watchlist:
    def __init__(self):
        self._lock = threading.Lock()
        self._index = faiss.IndexFlatIP(DIM)  # inner product == cosine on L2-normalised vectors
        self._meta: list[tuple[int, int, str]] = []  # (case_id, photo_id, kind)

    def rebuild(self):
        rows = db.query(
            "SELECT s.case_id, s.photo_id, s.kind, s.embedding FROM signatures s "
            "JOIN cases c ON c.id = s.case_id WHERE c.status = 'active'"
        )
        index = faiss.IndexFlatIP(DIM)
        meta = []
        if rows:
            vecs = np.stack([np.frombuffer(r["embedding"], np.float32) for r in rows])
            index.add(vecs)
            meta = [(r["case_id"], r["photo_id"], r["kind"]) for r in rows]
        with self._lock:
            self._index, self._meta = index, meta

    @property
    def size(self) -> int:
        return self._index.ntotal

    def search(self, embedding: np.ndarray, k: int = 64) -> list[CaseMatch]:
        """Return the best match per case, strongest first."""
        with self._lock:
            if self._index.ntotal == 0:
                return []
            sims, ids = self._index.search(embedding.reshape(1, -1).astype(np.float32), min(k, self._index.ntotal))
            meta = self._meta
        best: dict[int, CaseMatch] = {}
        for sim, idx in zip(sims[0], ids[0]):
            if idx < 0:
                continue
            case_id, photo_id, kind = meta[idx]
            sim = float(sim)
            adjusted = sim if kind == "real" else sim - GENERATED_PENALTY
            m = best.get(case_id)
            if m is None:
                m = best[case_id] = CaseMatch(case_id, adjusted, -1.0, photo_id, kind)
            elif adjusted > m.score:
                m.score, m.photo_id, m.kind = adjusted, photo_id, kind
            if kind == "real":
                m.real_score = max(m.real_score, sim)
        return sorted(best.values(), key=lambda m: m.score, reverse=True)


watchlist = Watchlist()
