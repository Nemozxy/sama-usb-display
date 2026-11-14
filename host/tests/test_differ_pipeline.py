import threading

import pytest

import differ_encoder as de
import image_encoder as ie
from differ_pipeline import DifferPipeline, DifferPlanner, PIXEL_BYTES, UpdatePlan
from idd_frame_bridge import DesktopFrame, LatestFrameMailbox


def solid(rgb):
    r, g, b = rgb
    return bytes((b, g, r, 255)) * (PIXEL_BYTES // 4)


def payload(plan):
    body = plan.data[250:]
    packed = b"".join(body[i:i + 249] for i in range(0, len(body), 250))
    length = int.from_bytes(plan.data[3:7], "big")
    return packed[:length]


def test_planner_initializes_then_sequences_reconstructable_deltas():
    planner = DifferPlanner()
    frames = []
    for value in (30, 50, 80):
        pixels = bytearray(solid((10, 20, 30)))
        pixels[40:44] = bytes((value, value, value, 255))
        frames.append(bytes(pixels))
    full = ie.plan_stream(ie.build_full_image(frames[0], frames=False))
    initial = planner.plan(frames[0], sequence=10)
    assert initial.kind == "initialize"
    assert initial.data == full + full
    first = planner.plan(frames[1], sequence=11)
    second = planner.plan(frames[2], sequence=12)
    assert first.kind == second.kind == "delta"
    assert (first.command_sequence, second.command_sequence) == (0, 1)
    assert de.apply_sequence_payload(frames[0], payload(first)) == frames[1]
    assert de.apply_sequence_payload(frames[1], payload(second)) == frames[2]
    sequences = []
    previous = frames[2]
    for value in (90, 100, 110):
        current = bytearray(previous)
        current[40:44] = bytes((value, value, value, 255))
        current = bytes(current)
        update = planner.plan(current, sequence=value)
        sequences.append(update.command_sequence)
        assert de.apply_sequence_payload(previous, payload(update)) == current
        previous = current
    assert sequences == [2, 3, 4]


def test_empty_delta_uses_vendor_marker():
    planner = DifferPlanner()
    pixels = solid((1, 2, 3))
    planner.plan(pixels, sequence=1)
    update = planner.plan(pixels, sequence=2)
    assert update.changed_pixels == 0
    assert payload(update) == de.SEQUENCE_EMPTY + de.MAGIC


@pytest.mark.parametrize("reason", ["large", "run"])
def test_full_fallback_forces_next_initialization(reason):
    planner = DifferPlanner()
    baseline = solid((0, 0, 0))
    planner.plan(baseline, sequence=1)
    if reason == "large":
        changed = solid((255, 255, 255))
    else:
        changed = bytearray(baseline)
        changed[:65001 * 4] = bytes((0, 0, 255, 255)) * 65001
        changed = bytes(changed)
    fallback = planner.plan(changed, sequence=2)
    assert fallback.kind == "full" and fallback.data[0] == 200
    assert fallback.command_sequence is None
    following = planner.plan(baseline, sequence=3)
    full = ie.plan_stream(ie.build_full_image(baseline, frames=False))
    assert following.kind == "initialize" and following.data == full + full
    small = bytearray(baseline)
    small[:4] = bytes((1, 2, 3, 255))
    delta = planner.plan(small, sequence=4)
    assert delta.kind == "delta" and delta.command_sequence == 0


def test_planner_rejects_wrong_geometry_without_changing_state():
    planner = DifferPlanner()
    with pytest.raises(ValueError, match="BGRA"):
        planner.plan(b"bad", sequence=1)
    assert planner.needs_init and planner.previous is None


def test_lossy_threshold_reconstructs_displayed_target_and_bounds_error():
    planner = DifferPlanner(change_threshold=8)
    baseline = solid((10, 20, 30))
    initial = planner.plan(baseline, sequence=1)
    assert initial.pixels == baseline
    current = bytearray(baseline)
    current[0:4] = bytes((38, 28, 18, 255))
    current[4:8] = bytes((39, 20, 10, 255))
    update = planner.plan(bytes(current), sequence=2)
    expected = bytearray(baseline)
    expected[4:8] = current[4:8]
    assert update.pixels == bytes(expected)
    assert de.apply_sequence_payload(baseline, payload(update)) == bytes(expected)
    assert abs(current[0] - update.pixels[0]) == 8
    assert update.pixels[4:8] == current[4:8]
    assert planner.previous == update.pixels


def test_planner_validates_optional_change_threshold():
    for threshold in (-1, 256):
        with pytest.raises(ValueError, match="0..255"):
            DifferPlanner(change_threshold=threshold)
    with pytest.raises(ValueError, match="nonnegative"):
        DifferPlanner(reinit_interval=-1)


def test_periodic_reinitialization_resets_delta_sequence():
    planner = DifferPlanner(reinit_interval=2)
    pixels = solid((1, 2, 3))
    plans = [planner.plan(pixels, sequence=sequence) for sequence in range(5)]
    assert [plan.kind for plan in plans] == [
        "initialize", "delta", "delta", "initialize", "delta"]
    assert [plan.command_sequence for plan in plans] == [None, 0, 1, None, 0]
    assert plans[3].data == plans[0].data


class TinyPlanner:
    def __init__(self, encoded=None):
        self.count = 0
        self.encoded = encoded

    def plan(self, pixels, *, sequence, timestamp_ns=0):
        self.count += 1
        if self.encoded is not None and sequence == 3:
            self.encoded.set()
        return UpdatePlan(sequence, timestamp_ns, "delta", b"x", b"", sequence - 1,
                          0, 0)


def test_encoding_overlaps_blocked_write_and_keeps_only_latest_pending():
    source, receipts = LatestFrameMailbox(), []
    pixels = solid((1, 2, 3))
    source.put(DesktopFrame(1, 1, pixels))
    began, release, encoded = threading.Event(), threading.Event(), threading.Event()
    sent = []

    def send(plan):
        sent.append(plan.sequence)
        began.set()
        assert release.wait(5)

    pipeline = DifferPipeline(send, planner=TinyPlanner(encoded))
    worker = threading.Thread(target=lambda: pipeline.forward(source, receipts))
    worker.start()
    assert began.wait(5)
    source.put(DesktopFrame(2, 2, pixels))
    source.put(DesktopFrame(3, 3, pixels))
    source.close()
    assert encoded.wait(5)
    assert pipeline.planner.count >= 2
    release.set()
    worker.join(5)
    assert not worker.is_alive() and pipeline.error is None
    assert sent == [1, 3]
    assert [receipt["sequence"] for receipt in receipts] == [1, 3]
    assert source.overwritten + pipeline.encoded_overwritten == 1


def test_writer_failure_aborts_source_and_never_retries():
    source, receipts = LatestFrameMailbox(), []
    source.put(DesktopFrame(1, 0, solid((1, 2, 3))))
    source.close()
    attempts = []

    def fail(plan):
        attempts.append(plan.sequence)
        raise OSError("lost")

    pipeline = DifferPipeline(fail, planner=TinyPlanner())
    with pytest.raises(OSError, match="lost"):
        pipeline.forward(source, receipts)
    assert attempts == [1] and receipts == []
    assert source.take() is None and pipeline.outbound.take() is None
    assert not pipeline.thread.is_alive()
