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


def detect_face_landmarks(image_bytes: bytes) -> Optional[dict]:
    """Return face geometry for the dominant face in an image, normalized to 0..1.

    Returns a dict::

        {
            "image_w": int, "image_h": int,
            "bbox":  {"x": float, "y": float, "w": float, "h": float},
            "mouth": {"cx": float, "cy": float, "w": float, "h": float},
            "eyes":  {"lx": float, "ly": float, "rx": float, "ry": float},
        }

    All geometric values are expressed as percentages of the source image
    (``0..1``) so the frontend can overlay them onto a responsive
    <img> without having to know the raw pixel size. Returns ``None``
    when no face is detected or the image can't be decoded.

    Why this lives here rather than in its own module: we already ship
    InsightFace for face *recognition*, and the same ``buffalo_l`` pack
    exposes 5-point keypoints (left eye, right eye, nose, left mouth
    corner, right mouth corner) as ``face.kps``. Reusing it means no
    extra native deps and no extra model download just to place a mouth.
    """
    import cv2

    analyzer = _build_analyzer()
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    faces = analyzer.get(img)
    if not faces:
        return None

    def area(f):
        x1, y1, x2, y2 = f.bbox
        return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))

    face = max(faces, key=area)
    img_h, img_w = img.shape[:2]

    def norm_xy(px: float, py: float) -> Tuple[float, float]:
        return (float(px) / img_w, float(py) / img_h)

    x1, y1, x2, y2 = face.bbox
    bx, by = norm_xy(x1, y1)
    bw = float(x2 - x1) / img_w
    bh = float(y2 - y1) / img_h

    kps = getattr(face, "kps", None)
    if kps is None or len(kps) < 5:
        return None
    # buffalo_l ordering: [left_eye, right_eye, nose, left_mouth, right_mouth]
    (lex, ley), (rex, rey) = norm_xy(*kps[0]), norm_xy(*kps[1])
    (lmx, lmy), (rmx, rmy) = norm_xy(*kps[3]), norm_xy(*kps[4])
    mouth_cx = (lmx + rmx) / 2.0
    mouth_cy = (lmy + rmy) / 2.0
    mouth_w = abs(rmx - lmx) * 1.9  # a touch wider than corner-to-corner
    # Height estimate: mouth tends to be ~35% as tall as it is wide for
    # a natural resting pose; we can't measure it from 5 keypoints
    # alone, so approximate and let the SVG morph handle the rest.
    mouth_h = mouth_w * 0.55

    return {
        "image_w": int(img_w),
        "image_h": int(img_h),
        "bbox":  {"x": bx, "y": by, "w": bw, "h": bh},
        "mouth": {"cx": mouth_cx, "cy": mouth_cy, "w": mouth_w, "h": mouth_h},
        "eyes":  {"lx": lex, "ly": ley, "rx": rex, "ry": rey},
    }


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

    Thin wrapper kept for backward compatibility — most callers should use
    :func:`rank` so they can also see the best candidate even when the
    similarity falls just under the threshold (useful for diagnostics in
    the live camera UI).
    """
    ranked = rank(probe, gallery)
    if not ranked:
        return None
    top_pid, top_score = ranked[0]
    if top_score < threshold:
        return None
    return RecognitionResult(person_id=top_pid, similarity=top_score)


def rank(
    probe: np.ndarray,
    gallery: List[EnrolledFace],
) -> List[Tuple[int, float]]:
    """Return ``[(person_id, best_similarity), ...]`` sorted high → low.

    We collapse every enrolled photo of a given person down to one
    combined score by taking the max similarity across that person's
    photos — the intuition is "the best shot of them wins", which is
    robust to angle variance in the enrolled gallery. The full ranked
    list lets callers expose "almost matched X" diagnostics rather than
    just a binary matched / not-matched.
    """
    if not gallery:
        return []
    mat = np.stack([g.embedding for g in gallery], axis=0)  # (N, 512)
    sims = mat @ probe  # (N,)
    best_by_person: dict[int, float] = {}
    for g, s in zip(gallery, sims):
        prev = best_by_person.get(g.person_id, -1.0)
        if s > prev:
            best_by_person[g.person_id] = float(s)
    return sorted(best_by_person.items(), key=lambda kv: kv[1], reverse=True)
