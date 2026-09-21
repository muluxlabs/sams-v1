"""
የፊት መለያ ሞተር — face recognition engine.

Model choice: InsightFace `buffalo_l` (ArcFace / SCRFD) through ONNX Runtime,
deliberately NOT the `face_recognition` / dlib stack that every tutorial
reaches for. The dlib embedding is older and its training data skews heavily
toward light-skinned Western faces; NIST's FRVT evaluations found error rates
varying by large factors across demographic groups, with the worst gaps in
exactly that class of algorithm. Every user of this system is Ethiopian.
Choosing dlib here would mean choosing the model most likely to fail on the
only people who matter.

Two rules the rest of the system depends on:

  * The threshold is CALIBRATED on the Woreda's own enrolled staff, never
    taken from a published default. calibrate() does this and reports the
    false-reject rate it costs.

  * The engine is biased toward refusing. A false accept means one employee
    marked another present — that is fraud, and it discredits every report.
    A false reject is an annoyance the PIN fallback solves in ten seconds.

The engine degrades gracefully: if insightface is not installed, everything
still imports and the non-recognition parts of the system run and test. The
kiosk reports the camera as unavailable in Amharic rather than crashing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

log = logging.getLogger(__name__)

EMBED_DIM = 512
DEFAULT_MODEL = "buffalo_l"

# A starting point only. calibrate() replaces it with a number measured on
# the actual enrolled staff, and the calibrated value lives in settings.
FALLBACK_THRESHOLD = 0.42

# Enrollment quality gates. A rushed enrollment produces a system that never
# works properly and nobody can explain why, so these are enforced hard.
MIN_FACE_WIDTH_PX = 110
MIN_SHARPNESS = 45.0      # variance of Laplacian
MAX_YAW_DEGREES = 22.0
MIN_BRIGHTNESS = 45
MAX_BRIGHTNESS = 215


class RecognitionUnavailable(RuntimeError):
    """insightface / onnxruntime are not installed or no model is present."""


@dataclass
class DetectedFace:
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray
    det_score: float
    quality: float
    width: int
    sharpness: float
    brightness: float
    yaw: float = 0.0

    @property
    def passes_enrollment_gate(self) -> bool:
        return (
            self.width >= MIN_FACE_WIDTH_PX
            and self.sharpness >= MIN_SHARPNESS
            and MIN_BRIGHTNESS <= self.brightness <= MAX_BRIGHTNESS
            and abs(self.yaw) <= MAX_YAW_DEGREES
        )

    def rejection_reason_am(self) -> str:
        """Why an enrollment capture was refused, in words HR can act on."""
        if self.width < MIN_FACE_WIDTH_PX:
            return "ፊቱ በጣም ሩቅ ነው። ወደ ካሜራው ይቅረቡ።"
        if self.sharpness < MIN_SHARPNESS:
            return "ምስሉ ደብዛዛ ነው። ሳይንቀሳቀሱ ይቆዩ።"
        if self.brightness < MIN_BRIGHTNESS:
            return "ብርሃኑ በቂ አይደለም። የክፍሉን ብርሃን ይጨምሩ።"
        if self.brightness > MAX_BRIGHTNESS:
            return "ብርሃኑ በዝቷል። ከመስኮት ወይም ከብርሃን ምንጭ ይራቁ።"
        if abs(self.yaw) > MAX_YAW_DEGREES:
            return "በቀጥታ ወደ ካሜራው ይመልከቱ።"
        return ""


@dataclass
class MatchResult:
    employee_id: int | None
    similarity: float
    threshold: float
    runner_up_id: int | None = None
    runner_up_similarity: float = 0.0

    @property
    def matched(self) -> bool:
        return self.employee_id is not None

    @property
    def margin(self) -> float:
        """
        Gap between the best and second-best candidate.

        A confident match is not just above the threshold — it is clearly
        ahead of everyone else. A narrow margin means two enrolled people
        look similar to the model, which is the situation that produces a
        wrong attendance record, so it is refused separately.
        """
        return round(self.similarity - self.runner_up_similarity, 4)


def normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(normalize(a), normalize(b)))


def pack_embedding(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class FaceGallery:
    """
    All enrolled embeddings in one matrix.

    At 30 employees with 5 images each this is a 150x512 float32 array —
    about 300KB, and a brute-force match is a single matrix multiply taking
    microseconds. No vector database, no index, nothing to go stale.
    """

    def __init__(self, threshold: float = FALLBACK_THRESHOLD,
                 min_margin: float = 0.05) -> None:
        self.threshold = threshold
        self.min_margin = min_margin
        self._matrix: np.ndarray = np.zeros((0, EMBED_DIM), dtype=np.float32)
        self._owners: list[int] = []

    def __len__(self) -> int:
        return len(self._owners)

    @property
    def employee_ids(self) -> set[int]:
        return set(self._owners)

    def load(self, rows: Sequence[tuple[int, bytes]]) -> None:
        """rows: (employee_id, packed embedding)."""
        vectors, owners = [], []
        for employee_id, blob in rows:
            v = unpack_embedding(blob)
            if v.shape[0] != EMBED_DIM:
                log.warning("skipping embedding of dim %s for employee %s",
                            v.shape[0], employee_id)
                continue
            vectors.append(normalize(v.astype(np.float32)))
            owners.append(employee_id)
        self._matrix = (
            np.vstack(vectors) if vectors
            else np.zeros((0, EMBED_DIM), dtype=np.float32)
        )
        self._owners = owners

    def match(self, embedding: np.ndarray) -> MatchResult:
        if len(self) == 0:
            return MatchResult(None, 0.0, self.threshold)

        q = normalize(np.asarray(embedding, dtype=np.float32))
        sims = self._matrix @ q

        # Best similarity per employee, not per image: five photos of one
        # person must not out-vote one photo of another.
        best: dict[int, float] = {}
        for owner, s in zip(self._owners, sims):
            if s > best.get(owner, -1.0):
                best[owner] = float(s)

        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top_sim = ranked[0]
        run_id, run_sim = (ranked[1] if len(ranked) > 1 else (None, 0.0))

        if top_sim < self.threshold:
            return MatchResult(None, top_sim, self.threshold, run_id, run_sim)
        if run_id is not None and (top_sim - run_sim) < self.min_margin:
            # Two enrolled people are too close to separate confidently.
            # Refusing costs ten seconds; guessing corrupts the payroll.
            return MatchResult(None, top_sim, self.threshold, run_id, run_sim)
        return MatchResult(top_id, top_sim, self.threshold, run_id, run_sim)

    # -- calibration ---------------------------------------------------

    def calibrate(
        self, target_false_accept: float = 0.0
    ) -> dict[str, float]:
        """
        Choose the threshold from the enrolled population itself.

        Compares every genuine pair (same person, different images) against
        every impostor pair, then picks the lowest threshold at which no
        impostor pair is accepted. Reports the false-reject rate that costs.

        If the false-reject rate comes back above about 2%, the answer is to
        re-enroll with better captures — never to lower the threshold.
        """
        if len(self) < 2:
            return {
                "threshold": self.threshold, "genuine_pairs": 0,
                "impostor_pairs": 0, "false_reject_rate": 0.0,
            }

        sims = self._matrix @ self._matrix.T
        genuine: list[float] = []
        impostor: list[float] = []
        n = len(self._owners)
        for i in range(n):
            for j in range(i + 1, n):
                (genuine if self._owners[i] == self._owners[j] else impostor).append(
                    float(sims[i, j])
                )

        if not impostor:
            return {
                "threshold": self.threshold,
                "genuine_pairs": len(genuine), "impostor_pairs": 0,
                "false_reject_rate": 0.0,
            }

        impostor.sort()
        idx = min(
            len(impostor) - 1,
            int(len(impostor) * (1 - target_false_accept)),
        )
        # Sit just above the highest impostor score we are willing to allow.
        chosen = round(impostor[idx] + 0.02, 4)
        chosen = float(min(max(chosen, 0.20), 0.85))

        frr = (
            sum(1 for g in genuine if g < chosen) / len(genuine)
            if genuine else 0.0
        )
        return {
            "threshold": chosen,
            "genuine_pairs": len(genuine),
            "impostor_pairs": len(impostor),
            "max_impostor_similarity": round(max(impostor), 4),
            "min_genuine_similarity": round(min(genuine), 4) if genuine else 0.0,
            "false_reject_rate": round(frr, 4),
        }


class FaceEngine:
    """Wraps InsightFace. Import-safe when the model is not installed."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        model_root: str | Path | None = None,
        det_size: tuple[int, int] = (640, 640),
    ) -> None:
        self.model_name = model_name
        self.model_root = str(model_root) if model_root else None
        self.det_size = det_size
        self._app: Any = None

    @property
    def available(self) -> bool:
        return self._app is not None

    def load(self) -> None:
        try:
            from insightface.app import FaceAnalysis
        except ImportError as e:  # pragma: no cover - depends on deployment
            raise RecognitionUnavailable(
                "insightface is not installed; run "
                "`pip install insightface onnxruntime`"
            ) from e

        kwargs: dict[str, Any] = {
            "name": self.model_name,
            "providers": ["CPUExecutionProvider"],
        }
        if self.model_root:
            kwargs["root"] = self.model_root
        app = FaceAnalysis(**kwargs)
        app.prepare(ctx_id=-1, det_size=self.det_size)
        self._app = app
        log.info("face engine ready: %s", self.model_name)

    def detect(self, image: np.ndarray) -> list[DetectedFace]:
        if self._app is None:
            raise RecognitionUnavailable("engine not loaded")

        faces = self._app.get(image)
        out: list[DetectedFace] = []
        for f in faces:
            x1, y1, x2, y2 = (int(v) for v in f.bbox)
            width = max(0, x2 - x1)
            crop = image[max(0, y1): max(0, y2), max(0, x1): max(0, x2)]
            sharp = _sharpness(crop)
            bright = _brightness(crop)
            yaw = float(getattr(f, "pose", [0, 0, 0])[1]) if hasattr(f, "pose") else 0.0
            out.append(
                DetectedFace(
                    bbox=(x1, y1, x2, y2),
                    embedding=normalize(np.asarray(f.embedding, dtype=np.float32)),
                    det_score=float(f.det_score),
                    quality=_quality_score(width, sharp, bright),
                    width=width,
                    sharpness=sharp,
                    brightness=bright,
                    yaw=yaw,
                )
            )
        out.sort(key=lambda d: d.width, reverse=True)
        return out

    def single_face(self, image: np.ndarray) -> tuple[DetectedFace | None, str]:
        """
        Exactly one face, or an Amharic explanation.

        Two faces in frame is refused rather than resolved by picking the
        largest: at a shared doorway that is precisely how one person gets
        marked present by another person walking past.
        """
        faces = self.detect(image)
        if not faces:
            return None, "ፊት አልተገኘም። በቀጥታ ወደ ካሜራው ይመልከቱ።"
        if len(faces) > 1:
            return None, "ከአንድ በላይ ፊት ታይቷል። አንድ በአንድ ይቅረቡ።"
        return faces[0], ""


def _sharpness(crop: np.ndarray) -> float:
    if crop.size == 0:
        return 0.0
    try:
        import cv2

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return float(np.var(crop.astype(np.float64)))


def _brightness(crop: np.ndarray) -> float:
    return float(np.mean(crop)) if crop.size else 0.0


def _quality_score(width: int, sharpness: float, brightness: float) -> float:
    w = min(1.0, width / 200.0)
    s = min(1.0, sharpness / 150.0)
    mid = 130.0
    b = max(0.0, 1.0 - abs(brightness - mid) / mid)
    return round(0.4 * w + 0.4 * s + 0.2 * b, 4)
