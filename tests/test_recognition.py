"""
Adversarial tests for the matching layer.

These test the gallery, thresholds, margin rule and calibration with
synthetic embeddings — no camera or model needed, so they run anywhere.
The parts that need a real face model (detection, liveness, spoofing) are
covered by the on-site pilot described in the build notes; they cannot be
honestly simulated here, and pretending otherwise would be worse than
admitting it.
"""

import numpy as np
import pytest

from sams.recognition.engine import (
    EMBED_DIM,
    DetectedFace,
    FaceGallery,
    cosine_similarity,
    normalize,
    pack_embedding,
    unpack_embedding,
)

rng = np.random.default_rng(20260920)


def person(seed: int, spread: float = 0.0, n: int = 1) -> list[np.ndarray]:
    """
    n embeddings of one identity; `spread` simulates capture variation.

    The noise is scaled by 1/sqrt(dim) so that `spread` is the norm of the
    perturbation rather than a per-dimension sigma. Without that scaling,
    a spread of 0.2 at 512 dimensions has norm 4.5 and completely swamps
    the unit-length identity vector — every "same person" pair would look
    like a stranger, which is not what a face model does.

    With this scaling, spread 0.8 gives a same-person cosine around 0.78,
    which is the right order for ArcFace embeddings. Two independent
    identities land near 0.0, as near-orthogonal random vectors do.
    """
    base = normalize(rng.standard_normal(EMBED_DIM))
    out = []
    for _ in range(n):
        noise = rng.standard_normal(EMBED_DIM) / np.sqrt(EMBED_DIM)
        out.append(normalize(base + spread * noise))
    return out


# -- packing round trip --------------------------------------------------


def test_embedding_survives_the_database_round_trip():
    v = person(1)[0]
    back = unpack_embedding(pack_embedding(v))
    assert back.shape == (EMBED_DIM,)
    assert np.allclose(v, back, atol=1e-6)


def test_wrong_dimension_embeddings_are_skipped():
    g = FaceGallery()
    g.load([(1, np.zeros(128, dtype=np.float32).tobytes())])
    assert len(g) == 0


# -- the empty and single-person cases -----------------------------------


def test_empty_gallery_matches_nobody():
    g = FaceGallery()
    assert g.match(person(1)[0]).matched is False


def test_gallery_with_one_person_still_applies_the_threshold():
    g = FaceGallery(threshold=0.4)
    g.load([(1, pack_embedding(v)) for v in person(1, n=1)])
    assert g.match(person(1)[0]).matched is False   # a different identity
    assert g.match(g._matrix[0]).employee_id == 1   # itself


# -- matching ------------------------------------------------------------


def test_same_person_matches_across_captures():
    vs = person(1, spread=0.8, n=5)
    g = FaceGallery(threshold=0.4)
    g.load([(1, pack_embedding(v)) for v in vs])
    probe = normalize(vs[0] + 0.5 * rng.standard_normal(EMBED_DIM) / np.sqrt(EMBED_DIM))
    assert g.match(probe).employee_id == 1


def test_stranger_is_refused():
    g = FaceGallery(threshold=0.4)
    rows = []
    for pid in range(1, 31):                       # the whole 30-person office
        rows += [(pid, pack_embedding(v)) for v in person(pid, spread=0.8, n=5)]
    g.load(rows)
    for _ in range(50):
        stranger = normalize(rng.standard_normal(EMBED_DIM))
        assert g.match(stranger).matched is False, "a stranger was accepted"


def test_best_image_per_person_wins_not_most_images():
    """
    Five photos of one person must not out-vote one photo of another —
    otherwise whoever enrolled most often wins every ambiguous match.
    """
    target = person(2, n=1)[0]
    rows = [(1, pack_embedding(v)) for v in person(1, spread=0.8, n=20)]
    rows.append((2, pack_embedding(target)))
    g = FaceGallery(threshold=0.3)
    g.load(rows)
    assert g.match(target).employee_id == 2


# -- the refusal bias ----------------------------------------------------


def test_similar_pair_is_refused_on_a_narrow_margin():
    """
    Two enrolled people the model finds similar. Guessing between them
    puts a wrong name on an attendance record, so the engine refuses and
    lets the PIN fallback handle it.
    """
    base = person(1, n=1)[0]
    twin = normalize(base + 0.10 * rng.standard_normal(EMBED_DIM) / np.sqrt(EMBED_DIM))
    g = FaceGallery(threshold=0.3, min_margin=0.15)
    g.load([(1, pack_embedding(base)), (2, pack_embedding(twin))])
    probe = normalize(base + 0.08 * rng.standard_normal(EMBED_DIM) / np.sqrt(EMBED_DIM))
    r = g.match(probe)
    assert r.matched is False
    assert r.margin < 0.15


def test_a_clear_winner_passes_the_margin_rule():
    g = FaceGallery(threshold=0.3, min_margin=0.05)
    rows = [(1, pack_embedding(v)) for v in person(1, spread=0.8, n=3)]
    rows += [(2, pack_embedding(v)) for v in person(2, spread=0.8, n=3)]
    g.load(rows)
    probe = unpack_embedding(pack_embedding(g._matrix[0]))
    assert g.match(probe).employee_id == 1


def test_raising_the_threshold_only_ever_refuses_more():
    """Monotonicity: a stricter threshold must never accept something a
    looser one rejected. Guards against sign or comparison errors."""
    rows = []
    for pid in range(1, 11):
        rows += [(pid, pack_embedding(v)) for v in person(pid, spread=0.8, n=3)]
    probes = [normalize(rng.standard_normal(EMBED_DIM)) for _ in range(30)]
    probes += [unpack_embedding(r[1]) for r in rows[:10]]

    accepted_at = {}
    for th in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        g = FaceGallery(threshold=th, min_margin=0.0)
        g.load(rows)
        accepted_at[th] = {
            i for i, p in enumerate(probes) if g.match(p).matched
        }
    ths = sorted(accepted_at)
    for lo, hi in zip(ths, ths[1:]):
        assert accepted_at[hi] <= accepted_at[lo]


def test_similarity_is_bounded():
    a, b = person(1)[0], person(2)[0]
    assert -1.0001 <= cosine_similarity(a, b) <= 1.0001
    assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-6)


def test_zero_vector_does_not_divide_by_zero():
    z = np.zeros(EMBED_DIM, dtype=np.float32)
    assert not np.isnan(normalize(z)).any()
    g = FaceGallery()
    g.load([(1, pack_embedding(person(1)[0]))])
    assert not np.isnan(g.match(z).similarity)


# -- calibration ---------------------------------------------------------


def test_calibration_separates_genuine_from_impostor():
    rows = []
    for pid in range(1, 31):
        rows += [(pid, pack_embedding(v)) for v in person(pid, spread=0.8, n=5)]
    g = FaceGallery()
    g.load(rows)
    stats = g.calibrate()
    assert stats["genuine_pairs"] == 30 * (5 * 4 // 2)
    assert stats["impostor_pairs"] > 0
    assert stats["threshold"] > stats["max_impostor_similarity"]
    assert stats["false_reject_rate"] <= 0.02, (
        "false-reject rate too high — re-enroll rather than lower the threshold"
    )


def test_calibrated_threshold_rejects_every_impostor_pair():
    rows = []
    for pid in range(1, 31):
        rows += [(pid, pack_embedding(v)) for v in person(pid, spread=0.8, n=5)]
    g = FaceGallery()
    g.load(rows)
    g.threshold = g.calibrate()["threshold"]
    g.min_margin = 0.0
    for _ in range(100):
        assert g.match(normalize(rng.standard_normal(EMBED_DIM))).matched is False


def test_calibration_is_safe_with_too_little_data():
    g = FaceGallery()
    g.load([(1, pack_embedding(person(1)[0]))])
    stats = g.calibrate()
    assert 0.2 <= stats["threshold"] <= 0.85


def test_calibrated_threshold_stays_in_a_sane_band():
    for spread in (0.2, 0.5, 0.8, 1.3):
        rows = []
        for pid in range(1, 11):
            rows += [(pid, pack_embedding(v)) for v in person(pid, spread=spread, n=4)]
        g = FaceGallery()
        g.load(rows)
        assert 0.2 <= g.calibrate()["threshold"] <= 0.85


# -- enrollment quality gate ---------------------------------------------


def good_face(**kw) -> DetectedFace:
    base = dict(
        bbox=(0, 0, 200, 200), embedding=person(1)[0], det_score=0.99,
        quality=0.9, width=200, sharpness=120.0, brightness=130.0, yaw=2.0,
    )
    base.update(kw)
    return DetectedFace(**base)


def test_a_good_capture_passes():
    assert good_face().passes_enrollment_gate
    assert good_face().rejection_reason_am() == ""


@pytest.mark.parametrize(
    "kw,fragment",
    [
        ({"width": 60}, "ይቅረቡ"),
        ({"sharpness": 10.0}, "ደብዛዛ"),
        ({"brightness": 20.0}, "ብርሃኑ በቂ አይደለም"),
        ({"brightness": 240.0}, "ብርሃኑ በዝቷል"),
        ({"yaw": 45.0}, "በቀጥታ"),
    ],
)
def test_bad_captures_are_refused_with_an_actionable_reason(kw, fragment):
    f = good_face(**kw)
    assert not f.passes_enrollment_gate
    assert fragment in f.rejection_reason_am()


def test_every_rejection_message_is_amharic():
    for kw in ({"width": 60}, {"sharpness": 5.0}, {"brightness": 10.0},
               {"brightness": 250.0}, {"yaw": 60.0}):
        msg = good_face(**kw).rejection_reason_am()
        assert any("ሀ" <= ch <= "፿" for ch in msg)
