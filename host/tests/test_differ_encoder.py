"""Byte-level tests for the cmd 208 position-info encoder.

Every expected byte below is HAND-DERIVED from the reconstructed algorithm in
`깬.cs:5412-5652`.  No observed device traffic is used, because none exists --
cmd 208 has never been transmitted to the panel in hand.  No invented golden
vectors: each one is annotated with the trace that produces it.

The tests that matter most are the ones asserting that the UNRESOLVED parts are
refused or explicitly flagged, rather than silently emitting plausible bytes.
"""

import pytest

import differ_encoder as de
from differ_encoder import (ALPHA_SKIP_THRESHOLD, CMD_SET_POS_INFO,
                            DEFAULT_MAX_RUN, FINAL_STRIDE, MAGIC, RECORD_BYTES,
                            NotVerified, build_cmd208_frames,
                            build_pos_info_payload, build_pos_info_records,
                            cmd208_value_matches_payload, describe_record,
                            parse_pos_info)


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------

def buf(alphas, rgb=(1, 2, 3)):
    """Build a flat 4-bytes-per-pixel BGRA buffer from a list of alpha values."""
    out = bytearray()
    for a in alphas:
        out += bytes((rgb[0], rgb[1], rgb[2], a))
    return bytes(out)


def rec(index, run):
    """The 5 bytes the algorithm should emit for one record."""
    return bytes(((index >> 16) & 0xFF, (index >> 8) & 0xFF, index & 0xFF,
                  (run >> 8) & 0xFF, run & 0xFF))


def one_row(alphas, **kw):
    """Treat the buffer as width=n, height=1."""
    return build_pos_info_records(buf(alphas), len(alphas), 1, **kw)


# ------------------------------------------------------------------------------------------
# constants
# ------------------------------------------------------------------------------------------

def test_record_is_five_bytes_not_six():
    """The live emitter uses a 5-byte record; the 6-byte one at 깬.cs:4574 is dead.

    This is the divergence the operator asked not to paper over: cmd 208's format
    is NOT image_encoder.rle_mask's format.
    """
    assert RECORD_BYTES == 5
    import image_encoder as ie
    assert ie.RLE_RUN_BYTES == 6
    assert RECORD_BYTES != ie.RLE_RUN_BYTES


def test_constants_match_the_vendor_code():
    assert FINAL_STRIDE == 10            # num3 += 10 at 깬.cs:5493
    assert DEFAULT_MAX_RUN == 65000      # A_5 default, 깬.cs:5412
    assert ALPHA_SKIP_THRESHOLD == 15    # A_0[...+3] <= 15, 깬.cs:5471
    assert CMD_SET_POS_INFO == 208       # 뽞.cs:6646
    assert MAGIC == b"\xEF\x69"          # 61289, 뽞.cs:6642-6645


def test_the_ten_byte_stride_arises_only_on_the_final_record():
    """There is no 10-byte record type: 5 is the record, 10 is the final stride."""
    assert FINAL_STRIDE == 2 * RECORD_BYTES


# ------------------------------------------------------------------------------------------
# hand-derived golden vectors
# ------------------------------------------------------------------------------------------

def test_three_changed_pixels_then_skips():
    """alphas [255,255,255,0,0,0,0,0,0,0] -> one record (index 0, run 3).

    trace: p=0..2 open a record at cursor 0 and count 1,2,3; p=3 is skipped, so the
    normal close writes run=3 and advances 5; the loop then ends with no open
    record, so there is no final stride and no gap.
    """
    info = one_row([255, 255, 255] + [0] * 7)
    assert info.records == rec(0, 3) == bytes((0x00, 0x00, 0x00, 0x00, 0x03))
    assert info.out_len == 5
    assert info.run_count == 1
    assert info.final_open is False
    assert info.final_gap == 0


def test_single_changed_pixel_in_the_middle():
    """alphas [0,0,255,0] -> one record (index 2, run 1)."""
    info = one_row([0, 0, 255, 0])
    assert info.records == bytes((0x00, 0x00, 0x02, 0x00, 0x01))
    assert info.out_len == 5
    assert info.final_open is False


def test_no_changed_pixels_produces_an_empty_stream():
    info = one_row([0] * 8)
    assert info.records == b""
    assert info.out_len == 0
    assert info.run_count == 0
    assert info.final_open is False


def test_alpha_threshold_is_inclusive_at_15():
    """`<= 15` skips: 15 is skipped, 16 is part of the update."""
    assert one_row([15]).records == b""
    assert one_row([15]).out_len == 0
    assert one_row([16]).records == rec(0, 1)
    assert one_row([16]).out_len == FINAL_STRIDE


def test_index_is_24_bit_big_endian():
    """A changed pixel at index 0x000123 must encode as 00 01 23."""
    info = one_row([0] * 0x123 + [255])
    assert info.records[:3] == bytes((0x00, 0x01, 0x23))
    assert info.records == rec(0x123, 1)


def test_run_is_16_bit_big_endian():
    """A run of 0x0102 must encode as 01 02."""
    info = one_row([255] * 0x102)
    assert info.records[3:5] == bytes((0x01, 0x02))
    assert describe_record(info.records[:5])["run"] == 0x102


# ------------------------------------------------------------------------------------------
# the final-record quirk
# ------------------------------------------------------------------------------------------

def test_frame_ending_with_an_open_record_gets_the_ten_byte_stride():
    """alphas all 255 -> the loop ends with the record still open ('set final').

    The vendor writes only the 5 record bytes but advances the cursor by 10, so
    out_len is 10 while 5 bytes were written: a 5-byte zero gap.
    """
    info = one_row([255] * 10)
    assert info.records == bytes((0x00, 0x00, 0x00, 0x00, 0x0A))
    assert info.run_count == 1
    assert info.final_open is True
    assert info.final_gap == 5
    assert info.out_len == 10 == len(info.records) + info.final_gap


def test_the_gap_is_zero_padding_not_written_data():
    """The bytes between the last record and out_len are never written by the vendor."""
    info = one_row([255] * 10)
    assert len(info.records) == 5
    assert info.records[3:5] == bytes((0x00, 0x0A))
    # nothing in the returned stream claims to be the gap
    assert b"\x00" * 5 not in info.records[5:]


def test_closed_final_record_has_no_gap():
    """If the last pixel is skipped the final record closes normally -> stride 5."""
    info = one_row([255, 255, 0])
    assert info.records == rec(0, 2)
    assert info.final_open is False
    assert info.final_gap == 0
    assert info.out_len == 5


def test_declared_length_matches_payload_with_and_without_final_gap():
    """The zero gap is part of the actual payload before the EF 69 terminator."""
    open_case = buf([255] * 10)
    assert cmd208_value_matches_payload(open_case, 10, 1) is True
    closed_case = buf([255, 255, 0])
    assert cmd208_value_matches_payload(closed_case, 3, 1) is True


# ------------------------------------------------------------------------------------------
# forced splits (the max_run guard)
# ------------------------------------------------------------------------------------------

def test_run_is_split_at_max_run_and_the_next_pixel_is_skipped():
    """max_run=4 over 10 changed pixels.

    trace: p=0..3 builds run 4; at p=3 the counter hits the cap so the vendor zeroes
    pixel 4's flag; p=4 is then skipped, closing the record with run=4 and advancing
    5; p=5 opens a new record; p=5..8 reaches run 4 again and zeroes pixel 9; p=9 is
    skipped, closing with run=4.  Both records were closed normally, so no gap.

    NOTE the forced split EXCLUDES the pixel whose flag it zeroed (indices 4 and 9
    are covered, but each new record starts one past the zeroed pixel).  Index 4 is
    not listed as a record start; it is covered by the first record's run of 4.
    """
    info = one_row([255] * 10, max_run=4)
    assert info.records == rec(0, 4) + rec(5, 4)
    assert info.run_count == 2
    assert info.final_open is False
    assert info.out_len == 10


def test_forced_split_zeroes_the_next_pixels_flag_in_a_copy():
    """The vendor mutates its INPUT array; by default we work on a copy."""
    src = buf([255] * 4)
    build_pos_info_records(src, 4, 1, max_run=2)
    assert src == buf([255] * 4), "the caller's buffer must be untouched"


def test_mutate_input_true_reproduces_the_vendor_side_effect():
    """opt-in flag replicates 깬.cs:5545 writing into the caller's array."""
    src = bytearray(buf([255] * 4))
    build_pos_info_records(src, 4, 1, max_run=2, mutate_input=True)
    assert src[2 * 4 + 3] == 0, "pixel 2's flag byte should have been zeroed"


def test_forced_split_on_the_final_pixel_is_flagged_not_hidden():
    """max_run=2 over 2 pixels: the split targets pixel 2, which does not exist.

    The vendor's `A_0[(num4 + 1) * A_4 + 3] = 0` would write one element past the
    end of the input array.  We skip the write and flag it.  The output is
    provably unchanged because the pixel loop ends immediately afterwards.
    """
    info = build_pos_info_records(buf([255, 255]), 2, 1, max_run=2)
    assert info.ended_with_partial is True
    assert info.records == rec(0, 2)
    assert info.final_open is True
    assert info.out_len == FINAL_STRIDE
    assert "OOB write skipped" in info.described()


def test_long_run_with_partial_split_combines_both_behaviours():
    """max_run=3 over 7 changed pixels.

    p=0..2 -> run 3, zeroes pixel 3; p=3 skipped -> close run 3; p=4 opens;
    p=4..6 -> run 3, zeroes pixel 7 which is out of range -> flagged; loop ends with
    the record open -> final stride 10.
    """
    info = one_row([255] * 7, max_run=3)
    assert info.ended_with_partial is True
    assert info.records == rec(0, 3) + rec(4, 3)
    assert info.final_open is True
    assert info.final_gap == 5
    assert info.out_len == 15


def test_max_run_of_one_skips_every_other_pixel():
    """max_run=1 shows the forced split's side effect most starkly.

    trace: p=0 opens a record, run=1 hits the cap, so pixel 1's flag is zeroed;
    p=1 is skipped and closes the record; p=2 opens the next one and zeroes pixel 3;
    p=3 skips and closes; p=4 opens a third record and the split targets pixel 5,
    which does not exist -> flagged, and the record stays open to the end.

    So with a cap of 1 the scheme can only describe indices 0, 2, 4 -- the pixels
    whose flags it zeroed are never listed as record starts.  3 records, not 5.
    """
    info = one_row([255] * 5, max_run=1)
    assert info.records == rec(0, 1) + rec(2, 1) + rec(4, 1)
    assert info.run_count == 3
    assert info.final_open is True
    assert info.ended_with_partial is True
    assert info.out_len == 20 == len(info.records) + info.final_gap


# ------------------------------------------------------------------------------------------
# geometry handling
# ------------------------------------------------------------------------------------------

def test_width_and_height_enter_only_as_their_product():
    """The vendor never uses width in an index expression.

    `A_0[num4 * A_4 + k]` (깬.cs:5471) addresses pixels flatly, and `A_1`/`A_2`
    appear only as `num9 = A_1 * A_2` (깬.cs:5641).  10 pixels must therefore give
    identical output for 10x1, 5x2, 2x5 and 1x10.
    """
    src = buf([255, 0, 255, 0, 255, 0, 255, 0, 255, 0])
    outs = {build_pos_info_records(src, w, h).records
            for (w, h) in ((10, 1), (5, 2), (2, 5), (1, 10))}
    assert len(outs) == 1


def test_the_source_is_treated_as_a_flat_buffer_with_no_row_padding():
    """Index p is read at src[p*bpp + 3], never src[(p/width)*stride + ...]."""
    # 4 pixels, stride exactly 4; padding-free by construction
    info = build_pos_info_records(buf([255, 255, 255, 255]), 2, 2)
    assert info.records == rec(0, 4)


def test_accepts_a_padded_larger_buffer_and_reads_only_the_first_n_pixels():
    src = buf([255] * 12)          # 12 pixels available
    info = build_pos_info_records(src, 2, 2)   # but only 4 are in range
    assert info.records == rec(0, 4)


# ------------------------------------------------------------------------------------------
# input validation
# ------------------------------------------------------------------------------------------

def test_only_bpp_four_is_accepted():
    """Every recovered call site passes 4; 쐓.cs:284 maps 4 to Format32bppArgb."""
    for bad in (1, 2, 3, 5, 8):
        with pytest.raises(ValueError) as ei:
            build_pos_info_records(buf([255]), 1, 1, bpp=bad)
        assert "bpp" in str(ei.value)


def test_short_source_is_rejected_rather_than_read_past_the_end():
    with pytest.raises(ValueError) as ei:
        build_pos_info_records(buf([255] * 3), 4, 1)
    assert "too short" in str(ei.value)


def test_bad_max_run_is_rejected():
    with pytest.raises(ValueError):
        build_pos_info_records(buf([255]), 1, 1, max_run=0)


# ------------------------------------------------------------------------------------------
# payload assembly (records + EF 69)
# ------------------------------------------------------------------------------------------

def test_payload_appends_the_ef69_terminator():
    payload, declared, info = build_pos_info_payload(buf([255, 255, 0]), 3, 1)
    assert payload == rec(0, 2) + MAGIC
    assert declared == info.out_len + 2 == 7
    assert declared == len(payload)


def test_payload_declared_length_includes_the_gap_when_final_is_open():
    """The vendor writes EF 69 at out_len, leaving five zero bytes before it."""
    payload, declared, info = build_pos_info_payload(buf([255] * 10), 10, 1)
    assert info.final_open is True
    assert declared == 12
    assert payload == rec(0, 10) + bytes(5) + MAGIC
    assert len(payload) == declared == 12


def test_payload_never_extends_past_the_magic():
    for alphas in ([0] * 4, [255] * 4, [255, 0, 255, 0]):
        payload, _, _ = build_pos_info_payload(buf(alphas), len(alphas), 1)
        assert payload.endswith(MAGIC)
        assert payload.count(MAGIC) == 1


# ------------------------------------------------------------------------------------------
# framing refuses to be usable
# ------------------------------------------------------------------------------------------

def test_cmd208_frames_refuse_by_default():
    with pytest.raises(NotVerified) as ei:
        build_cmd208_frames(buf([255] * 10), 10, 1)
    msg = str(ei.value)
    assert "never been sent" in msg
    assert "gap" in msg


def test_cmd208_refusal_names_the_ordered_risk():
    with pytest.raises(NotVerified) as ei:
        build_cmd208_frames(buf([255] * 10), 10, 1)
    assert "202" in str(ei.value), "the 202/208 pairing must be named as inferred"


def test_cmd208_frames_only_build_when_forced_for_offline_inspection():
    frames = build_cmd208_frames(buf([255] * 10), 10, 1, allow_unverified=True)
    import serial_proto as sp
    assert len(frames) >= 2
    cmd = frames[0]
    assert len(cmd) == sp.FRAME_SIZE == 250
    assert cmd[0] == CMD_SET_POS_INFO == 208
    assert cmd[1:3] == MAGIC                  # EF 69 magic at [1..2]
    assert cmd[7] == 0                        # extra
    assert cmd[8] == 0 and cmd[9] == 0        # the [8..9] hole
    assert cmd[3:7] == (12).to_bytes(4, "big")   # declared length, BE32


def test_cmd208_data_frames_carry_the_payload_in_249_byte_chunks():
    import image_encoder as ie
    frames = build_cmd208_frames(buf([255] * 10), 10, 1, allow_unverified=True)
    body = b"".join(frames[1:])
    assert len(frames[1]) == ie.CHUNK_FRAME
    assert body[:12] == rec(0, 10) + bytes(5) + MAGIC
    assert set(body[12:]) == {0}               # the rest of the frame is padding


def test_forced_frames_are_the_only_way_to_get_bytes():
    """Guard: no module-level helper hands out cmd 208 bytes implicitly."""
    import inspect
    src = inspect.getsource(de)
    assert "allow_unverified: bool = False" in src
    assert "raise NotVerified(" in src


# ------------------------------------------------------------------------------------------
# decoding helpers
# ------------------------------------------------------------------------------------------

def test_describe_record_decodes_the_two_fields():
    assert describe_record(rec(0x123, 0x456)) == {"index": 0x123, "run": 0x456}


def test_describe_record_rejects_a_wrong_length():
    for bad in (b"", b"\x00" * 4, b"\x00" * 6):
        with pytest.raises(ValueError):
            describe_record(bad)


def test_parse_pos_info_round_trips_a_two_record_stream():
    src = buf([255] * 10)
    payload, _, info = build_pos_info_payload(src, 10, 1, max_run=4)
    got = parse_pos_info(payload)
    assert got == [{"index": 0, "run": 4}, {"index": 5, "run": 4}]
    assert len(got) == info.run_count


def test_parse_pos_info_requires_the_terminator():
    with pytest.raises(ValueError):
        parse_pos_info(rec(0, 1), expect_magic=True)


def test_parse_pos_info_reports_a_non_multiple_body():
    """A body that is not a whole number of records must be rejected, not guessed.

    A 3-byte gap makes the body 5 + 3 = 8 bytes, which is not a multiple of 5.
    (A 5-byte gap would land on exactly two records and is therefore NOT
    detectable this way -- which is precisely why the remaining gap is flagged as
    unresolved rather than silently accepted.)
    """
    with pytest.raises(ValueError) as ei:
        parse_pos_info(rec(0, 1) + b"\x00" * 3 + MAGIC)
    assert "not a multiple" in str(ei.value)


def test_a_five_byte_gap_is_indistinguishable_from_a_real_second_record():
    """Why the final-record quirk is a genuine ambiguity, demonstrated.

    a 5-byte gap decodes as a legitimate record (index 0, run 0), so a consumer
    cannot tell padding from data by the record size alone.
    """
    payload = rec(0, 1) + b"\x00" * 5 + MAGIC
    parsed = parse_pos_info(payload)
    assert len(parsed) == 2
    assert parsed[1] == {"index": 0, "run": 0}   # the gap read as a record


# ------------------------------------------------------------------------------------------
# safety / hygiene
# ------------------------------------------------------------------------------------------

def test_module_does_not_import_pyserial_or_open_a_port():
    """Hard constraint: this module must never touch hardware.

    Note `serial_proto` is the project's own protocol module (250-byte frames), not
    pyserial, so the check is on pyserial and on port-opening APIs specifically.
    """
    import inspect
    src = inspect.getsource(de)
    lowered = src.lower()
    for forbidden in ("pyserial", "serial.serial", "serialport", "comport",
                      "com_ports_for", "serialtransport", "pyserial"):
        assert forbidden not in lowered, f"unexpected: {forbidden}"
    assert "arch" + "=" not in lowered        # no space for a port arg either
    # the project's protocol module is fine, but only imported lazily
    assert "import serial_proto" in src


def test_cmd208_imports_are_deferred():
    """The heavy protocol imports only happen inside the opt-in builder."""
    import inspect
    src = inspect.getsource(de)
    head = src.split("def build_cmd208_frames")[0]
    assert "import serial_proto" not in head
    assert "import image_encoder" not in head


def test_result_is_describable_for_logging():
    info = one_row([255] * 10)
    txt = info.described()
    assert "records=1" in txt and "gap=5" in txt and "final_open=True" in txt
