"""InsightFace wrapper: enroll + recognize faces for a family.

The admin marks person_photos with ``use_for_face_recognition=True``. This
module walks those photos, extracts the dominant face embedding from each
one, and persists the 512-dim vector to ``face_embeddings``. At runtime
we load every embedding for a family into memory and compare incoming
webcam frames with cosine similarity.

Design notes
------------
* **Lazy init.** The InsightFace model pack is ~300 MB and takes a few
  seconds to load, so we build the analyzer on first use inside each
  worker process and cache it on the module.
* **Apple Silicon.** When ``AI_MAC_STUDIO_OPTIMIZED`` is true we ask
  onnxruntime for the CoreML execution provider first (runs on the ANE /
  GPU). The helper auto-falls-back to CPU when CoreML isn't compiled in,
  so the same code path works on a plain Linux box.
* **Single face per photo.** For enrollment we keep only the largest
  detected face — admins are expected to upload reasonably-cropped
  portraits.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..config import get_settings

logger = logging.getLogger(__name__)


# InsightFace and onnxruntime pull in NumPy, OpenCV, ONNX graphs and a
# handful of native libs on import. We don't want to pay that cost until
# something actually asks for recognition, so the heavy imports live
# inside ``_build_analyzer`` below.
_analyzer = None
_analyzer_lock = threading.Lock()
_EMBEDDING_DIM = 512


@dataclass
class EnrolledFace:
    """In-memory row used by the recognizer."""

    person_id: int
    embedding: np.ndarray  # L2-normalized float32, shape (512,)


@dataclass
class RecognitionResult:
    person_id: int
    similarity: float


def _providers() -> List[str]:
    """Pick the right onnxruntime execution providers for this host."""
    import onnxruntime as ort

    available = set(ort.get_available_providers())
    s = get_settings()
    ordered: List[str] = []
    if s.AI_MAC_STUDIO_OPTIMIZED and "CoreMLExecutionProvider" in available:
        ordered.append("CoreMLExecutionProvider")
    # CUDA is extremely unlikely on a Mac but doesn't hurt to honor if
    # someone runs this on a Linux workstation with an NVIDIA GPU.
    if "CUDAExecutionProvider" in available:
        ordered.append("CUDAExecutionProvider")
    ordered.append("CPUExecutionProvider")
    # De-dupe while preserving order.
    seen = set()
    return [p for p in ordered if not (p in seen or seen.add(p))]


def _build_analyzer():
    """Load InsightFace `buffalo_l` pack with the best-available provider."""
    global _analyzer
    with _analyzer_lock:
        if _analyzer is not None:
            return _analyzer

        # Heavy native imports deferred until first real use.
        from insightface.app import FaceAnalysis

        providers = _providers()
        logger.info("Loading InsightFace buffalo_l with providers=%s", providers)

        home = get_settings().AI_INSIGHTFACE_HOME
        kwargs = {"name": "buffalo_l", "providers": providers}
        if home:
            kwargs["root"] = home
        analyzer = FaceAnalysis(**kwargs)
        analyzer.prepare(ctx_id=0, det_size=(640, 640))
        _analyzer = analyzer
        return _analyzer


def providers_in_use() -> List[str]:
    """Public helper for the status endpoint."""
    try:
        import onnxruntime as ort

        return _providers() if ort else []
    except Exception:
        return []


def _normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalize so we can use a plain dot product for cosine similarity."""
    n = np.linalg.norm(v)
    if n < 1e-9:
        return v
    return (v / n).astype(np.float32, copy=False)


def extract_embedding(
    image_bytes: bytes,
) -> Optional[Tuple[np.ndarray, Tuple[int, int, int, int]]]:
    """Return ``(embedding, bounding_box)`` for the dominant face, or None.

    ``embedding`` is L2-normalized float32, shape (512,).
    """
    import cv2  # ships with opencv-python-headless

    analyzer = _build_analyzer()
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    faces = analyzer.get(img)
    if not faces:
        return None
    # Largest bounding-box area wins — best proxy for "the subject".
    def area(f):
        x1, y1, x2, y2 = f.bbox
        return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))

    face = max(faces, key=area)
    emb = np.asarray(face.normed_embedding, dtype=np.float32)
    # normed_embedding is already L2-normalized but we be defensive.
    emb = _normalize(emb)
    x1, y1, x2, y2 = (int(v) for v in face.bbox)
    return emb, (x1, y1, x2, y2)


def encode_bytes(embedding: np.ndarray) -> bytes:
    arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if arr.shape[0] != _EMBEDDING_DIM:
        raise ValueError(
            f"Expected {_EMBEDDING_DIM}-dim embedding, got {arr.shape[0]}"
        )
    return arr.tobytes()


def decode_bytes(b: bytes) -> np.ndarray:
    arr = np.frombuffer(b, dtype=np.float32)
    if arr.shape[0] != _EMBEDDING_DIM:
        raise ValueError(
            f"Stored embedding has unexpected dim {arr.shape[0]}"
        )
    return arr


def bbox_to_json(bbox: Tuple[int, int, int, int]) -> str:
    return json.dumps(list(bbox))


def match(
    probe: np.ndarray,
    gallery: List[EnrolledFace],
    threshold: float,
) -> Optional[RecognitionResult]:
    """Best-match nearest neighbor in ``gallery`` above ``threshold``.

    We collapse every enrolled photo of a given person down to one combined
    score by taking the max similarity across that person's photos — the
    intuition is "the best shot of them wins", which is robust to angle
    variance in the enrolled gallery.
    """
    if not gallery:
        return None
    # Stack for a single vectorized dot product.
    mat = np.stack([g.embedding for g in gallery], axis=0)  # (N, 512)
    sims = mat @ probe  # (N,)
    # Per-person best score.
    best_by_person: dict[int, float] = {}
    for g, s in zip(gallery, sims):
        prev = best_by_person.get(g.person_id, -1.0)
        if s > prev:
            best_by_person[g.person_id] = float(s)
    if not best_by_person:
        return None
    winner_pid = max(best_by_person, key=best_by_person.get)
    winner_score = best_by_person[winner_pid]
    if winner_score < threshold:
        return None
    return RecognitionResult(person_id=winner_pid, similarity=winner_score)
