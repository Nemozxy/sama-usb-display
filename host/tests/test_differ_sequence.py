"""Hand-derived cmd 204 vectors from 깬.cs:6134-6432 and 뽞.cs:6769-6960."""

import random

import pytest

import differ_encoder as de
import image_encoder as ie
import serial_proto as sp


def frame(*pixels):
    return b"".join(bytes(pixel) for pixel in pixels)


def pixel(value=0):
    return (value, value + 1, value + 2, 255)


def record(index, pixels):
    body = frame(*pixels)
    if len(pixels) == 1:
        return bytes((0x80 | (index >> 16), (index >> 8) & 0xFF, index & 0xFF)) + body
    return (index.to_bytes(3, "big") + len(pixels).to_bytes(2, "big") + body)


def test_one_changed_pixel_has_be24_index_be16_run_and_new_bgra():
    old = frame(pixel(0), pixel(10), pixel(20), pixel(30))
    new = frame(pixel(0), pixel(10), (1, 2, 3, 4), pixel(30))
    delta = de.build_sequence_delta(old, new)
    assert delta.records == record(2, [(1, 2, 3, 4)])
    assert delta.payload == delta.records + b"\xef\x69"
    assert delta.changed_pixels == 1
    assert delta.run_count == 1


def test_single_pixel_uses_the_vendor_seven_byte_flagged_record():
    old = frame(pixel(0), pixel(10), pixel(20))
    new = frame(pixel(0), (1, 2, 3, 4), pixel(20))
    delta = de.build_sequence_delta(old, new)
    assert delta.records == b"\x80\x00\x01\x01\x02\x03\x04"
    assert de.apply_sequence_payload(old, delta.payload) == new


def test_consecutive_changes_share_one_header():
    old = frame(*[pixel(0)] * 4)
    new = frame(pixel(0), (10, 11, 12, 13), (20, 21, 22, 23), pixel(0))
    delta = de.build_sequence_delta(old, new)
    assert delta.records == record(1, [(10, 11, 12, 13), (20, 21, 22, 23)])
    assert delta.run_count == 2 - 1


def test_disjoint_changes_and_final_open_run_are_closed():
    old = frame(*[pixel(0)] * 7)
    new_pixels = [pixel(0)] * 7
    new_pixels[1] = (1, 2, 3, 4)
    new_pixels[4] = (5, 6, 7, 8)
    new_pixels[5] = (9, 10, 11, 12)
    new_pixels[6] = (13, 14, 15, 16)
    delta = de.build_sequence_delta(old, frame(*new_pixels))
    assert delta.records == (record(1, [(1, 2, 3, 4)])
                             + record(4, [(5, 6, 7, 8), (9, 10, 11, 12),
                                          (13, 14, 15, 16)]))
    assert delta.run_count == 2
    assert delta.changed_pixels == 4


@pytest.mark.parametrize("channel", range(4))
def test_every_bgra_channel_participates_in_comparison(channel):
    old_pixel = (10, 20, 30, 40)
    new_pixel = list(old_pixel)
    new_pixel[channel] += 1
    delta = de.build_sequence_delta(frame(old_pixel), frame(tuple(new_pixel)))
    assert delta.records == record(0, [tuple(new_pixel)])


def test_no_change_uses_the_vendor_six_byte_marker_plus_magic():
    old = frame(pixel(1), pixel(2))
    delta = de.build_sequence_delta(old, old)
    assert delta.records == b""
    assert delta.changed_pixels == 0
    assert delta.run_count == 0
    assert delta.payload == b"\x80\x00\x00\x00\x00\x00\xef\x69"


def test_cmd204_embeds_big_endian_sequence_and_zero_position_length():
    old = frame(pixel(1))
    frames = de.build_cmd204_frames(old, old, sequence=0x01020304,
                                    allow_unverified=True)
    command = sp.parse_frame(frames[0])
    assert command.cmd == 204
    assert command.value == 8
    assert command.extra == 0
    assert command.payload[:8] == b"\x01\x02\x03\x04\x00\x00\x00\x00"
    assert b"".join(frames[1:])[:8] == de.SEQUENCE_EMPTY + de.MAGIC


def test_cmd204_data_uses_249_byte_chunks_padded_to_250():
    old = frame(*[pixel(0)] * 100)
    new = frame(*[pixel(1)] * 100)
    delta = de.build_sequence_delta(old, new)
    assert len(delta.payload) == 5 + 100 * 4 + 2 == 407
    frames = de.build_cmd204_frames(old, new, allow_unverified=True)
    assert len(frames) == 3
    assert all(len(item) == 250 for item in frames)
    assert frames[1][:249] == delta.payload[:249]
    assert frames[1][249] == 0
    assert frames[2][:158] == delta.payload[249:]
    assert frames[2][158:] == bytes(250 - 158)


def test_cmd204_refuses_implicit_hardware_use():
    old = frame(pixel(1))
    with pytest.raises(de.NotVerified, match="offline"):
        de.build_cmd204_frames(old, old)


def test_changed_run_over_65000_is_rejected_like_the_vendor_null_result():
    old = bytes(4 * 3)
    new = bytes([1]) * (4 * 3)
    with pytest.raises(ValueError, match="vendor rejects"):
        de.build_sequence_delta(old, new, max_run=2)


def test_disjoint_short_runs_do_not_trigger_the_run_limit():
    old = frame(pixel(0), pixel(0), pixel(0))
    new = frame(pixel(1), pixel(0), pixel(1))
    delta = de.build_sequence_delta(old, new, max_run=1)
    assert delta.run_count == 2


@pytest.mark.parametrize("old,new", [
    (b"", b"\x00"),
    (b"\x00\x00\x00", b"\x00\x00\x00"),
])
def test_invalid_frame_shapes_are_rejected(old, new):
    with pytest.raises(ValueError):
        de.build_sequence_delta(old, new)


def test_pixel_count_must_fit_the_be24_index():
    with pytest.raises(ValueError, match="24 bits"):
        de.build_sequence_delta(bytes(4 * 0x1000001), bytes(4 * 0x1000001))


def test_sequence_and_bpp_are_validated():
    old = frame(pixel(1))
    with pytest.raises(ValueError, match="32 bits"):
        de.build_cmd204_frames(old, old, sequence=0x100000000,
                               allow_unverified=True)
    with pytest.raises(ValueError, match="BGRA"):
        de.build_sequence_delta(old, old, bpp=3)


def test_independent_decoder_reconstructs_multiple_deterministic_frames():
    rng = random.Random(20261026)
    previous = bytes(rng.randrange(256) for _ in range(32 * 20 * 4))
    for _ in range(8):
        current = bytearray(previous)
        for _ in range(7):
            start = rng.randrange(32 * 20) * 4
            current[start:start + 4] = bytes(rng.randrange(256) for _ in range(4))
        current = bytes(current)
        delta = de.build_sequence_delta(previous, current)
        assert de.apply_sequence_payload(previous, delta.payload) == current
        previous = current


def test_numpy_fast_path_matches_scalar_reference():
    np = pytest.importorskip("numpy")
    rng = random.Random(204)
    previous = bytes(rng.randrange(256) for _ in range(1000 * 4))
    current = bytearray(previous)
    for start, length in ((0, 3), (100, 1), (400, 20), (999, 1)):
        for p in range(start, start + length):
            current[p * 4:p * 4 + 4] = bytes(rng.randrange(256) for _ in range(4))
    expected = de._build_sequence_delta_scalar(previous, current, 65000)
    actual = de._build_sequence_delta_numpy(previous, current, 65000, np)
    assert actual == expected


def test_decoder_reconstructs_no_change_without_touching_pixels():
    old = frame(pixel(3), pixel(4))
    assert de.apply_sequence_payload(old, de.SEQUENCE_EMPTY + de.MAGIC) == old


@pytest.mark.parametrize("payload", [
    b"",
    b"\x00\x00",
    b"\x00\x00\x00\x00\x01" + b"\xef\x69",
    b"\x00\x00\x01\x00\x01\x01\x02\x03" + b"\xef\x69",
    b"\x00\x00\x02\x00\x01\x01\x02\x03\x04" + b"\xef\x69",
])
def test_decoder_rejects_truncated_or_out_of_bounds_payloads(payload):
    with pytest.raises(ValueError):
        de.apply_sequence_payload(frame(pixel(1)), payload)


def test_small_delta_is_smaller_on_the_wire_than_cmd200():
    old = frame(*[pixel(0)] * 1000)
    new_pixels = [pixel(0)] * 1000
    new_pixels[500] = pixel(9)
    new = frame(*new_pixels)
    delta = de.build_sequence_delta(old, new)
    delta_wire = 250 + ie.chunked_wire_size(len(delta.payload))
    full_wire = 250 + ie.chunked_wire_size(len(new))
    assert delta_wire == 500
    assert full_wire == 4500
    assert delta_wire < full_wire


def test_worst_case_delta_is_larger_and_needs_full_frame_fallback():
    old = bytes(4 * 249)
    new = bytes([1]) * (4 * 249)
    delta = de.build_sequence_delta(old, new)
    delta_wire = 250 + ie.chunked_wire_size(len(delta.payload))
    full_wire = 250 + ie.chunked_wire_size(len(new))
    assert delta_wire == 1500
    assert full_wire == 1250
    assert delta_wire > full_wire


def test_precomputed_delta_framing_matches_without_reencoding():
    old = frame(pixel(1), pixel(2))
    new = frame(pixel(1), pixel(9))
    delta = de.build_sequence_delta(old, new)
    assert de.build_cmd204_delta_frames(
        delta, sequence=7, allow_unverified=True) == de.build_cmd204_frames(
            old, new, sequence=7, allow_unverified=True)
    with pytest.raises(de.NotVerified):
        de.build_cmd204_delta_frames(delta)


def test_precomputed_delta_fast_stream_is_byte_identical_to_frames():
    old = frame(*[pixel(1)] * 1000)
    new = frame(*[pixel(9)] * 1000)
    delta = de.build_sequence_delta(old, new)
    expected = b"".join(de.build_cmd204_delta_frames(
        delta, sequence=7, allow_unverified=True))
    actual = de.build_cmd204_delta_stream(delta, sequence=7, allow_unverified=True)
    assert actual == expected
    with pytest.raises(de.NotVerified):
        de.build_cmd204_delta_stream(delta)


def test_small_change_filter_uses_max_bgr_error_and_exact_alpha():
    previous = frame((10, 20, 30, 40), (50, 60, 70, 80), (1, 2, 3, 4))
    current = frame((14, 24, 34, 40), (55, 60, 70, 80), (1, 2, 3, 5))
    filtered = de.suppress_small_changes(previous, current, 4)
    assert filtered == frame(previous[:4], current[4:8], current[8:12])
    assert de.suppress_small_changes(previous, current, 0) == current


def test_small_change_filter_numpy_matches_scalar_reference():
    np = pytest.importorskip("numpy")
    rng = random.Random(205)
    previous = bytes(rng.randrange(256) for _ in range(1000 * 4))
    current = bytes(rng.randrange(256) for _ in range(1000 * 4))
    expected = de._suppress_small_changes_scalar(previous, current, 17)
    actual = de._suppress_small_changes_numpy(previous, current, 17, np)
    assert actual == expected


def test_small_change_filter_rejects_invalid_frames_and_thresholds():
    old = frame(pixel(1))
    with pytest.raises(ValueError, match="lengths differ"):
        de.suppress_small_changes(old, b"", 1)
    with pytest.raises(ValueError, match="whole BGRA"):
        de.suppress_small_changes(b"\x00", b"\x00", 1)
    for threshold in (-1, 256):
        with pytest.raises(ValueError, match="0..255"):
            de.suppress_small_changes(old, old, threshold)


def test_filtered_delta_fused_path_matches_separate_filter_and_encoder():
    rng = random.Random(206)
    previous = bytes(rng.randrange(256) for _ in range(1000 * 4))
    current = bytes(rng.randrange(256) for _ in range(1000 * 4))
    expected_pixels = de._suppress_small_changes_scalar(previous, current, 17)
    expected_delta = de._build_sequence_delta_scalar(previous, expected_pixels, 65000)
    delta, filtered = de.build_filtered_sequence_delta(previous, current, 17)
    assert filtered == expected_pixels
    assert delta == expected_delta
    assert de.apply_sequence_payload(previous, delta.payload) == filtered


def test_filtered_delta_zero_threshold_is_the_exact_path():
    old = frame(pixel(1), pixel(2))
    new = frame(pixel(3), pixel(4))
    delta, filtered = de.build_filtered_sequence_delta(old, new, 0)
    assert filtered == new
    assert delta == de.build_sequence_delta(old, new)
