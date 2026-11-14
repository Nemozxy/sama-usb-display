"""Tests for the serial (CH552 / rev-C) family protocol.

Byte-exactness against the vendor code is asserted here; acceptance by a real
device is NOT claimed and cannot be until hardware confirms it.
"""

import struct

import pytest

import serial_proto as sp
from serial_proto import (FRAME_SIZE, HELLO_PAYLOAD, MAGIC, PAYLOAD_OFFSET,
                          SerialCmd, build_brightness, build_frame, build_hello,
                          build_rotate, decode_reply, parse_frame)


# --------------------------------------------------------------------------
# frame geometry
# --------------------------------------------------------------------------

def test_frame_is_always_250_bytes():
    assert FRAME_SIZE == 250
    for n in (0, 1, 2, 100, 240):
        assert len(build_frame(7, 1, bytes(n))) == FRAME_SIZE


def test_magic_is_0xef69_and_lands_at_offsets_1_and_2():
    assert MAGIC == bytes((0xEF, 0x69))
    assert sp.MAGIC_WORD == 61289 == 0xEF69
    f = build_frame(0xAB, 0)
    assert f[1] == 0xEF and f[2] == 0x69


def test_cmd_is_byte_zero():
    assert build_frame(0x01, 0)[0] == 0x01
    assert build_frame(123, 0)[0] == 123


def test_value_is_big_endian_uint32_at_3_to_6():
    for v in (0, 1, 0xFF, 0x0100, 0x12345678, 0xFFFFFFFF):
        f = build_frame(9, v)
        assert f[3:7] == struct.pack(">I", v)
        assert parse_frame(f).value == v


def test_offsets_8_and_9_are_a_permanent_zero_hole():
    """The vendor never assigns array[8]/array[9] -- they must stay 0x00."""
    for cmd, val, pay, extra in ((1, 1, HELLO_PAYLOAD, 0),
                                 (123, 1, b"\x50", 0),
                                 (255, 0xDEADBEEF, bytes(50), 0x77)):
        f = build_frame(cmd, val, pay, extra)
        assert f[8] == 0 and f[9] == 0, "the [8..9] hole was written"


def test_extra_byte_at_offset_7():
    assert build_frame(5, 0, b"", 0x42)[7] == 0x42


def test_payload_starts_at_offset_10_and_is_verbatim():
    pay = bytes(range(40))
    f = build_frame(0xCC, 0, pay)
    assert f[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(pay)] == pay
    assert parse_frame(f).payload[:len(pay)] == pay


def test_frame_is_zero_padded_after_the_payload():
    pay = b"\xAA\xBB"
    f = build_frame(1, 1, pay)
    assert f[PAYLOAD_OFFSET:PAYLOAD_OFFSET + 2] == pay
    assert set(f[PAYLOAD_OFFSET + 2:]) == {0}


def test_no_checksum_bytes_are_appended():
    """A one-byte payload change must change exactly one byte of the frame."""
    a = build_frame(123, 1, b"\x10")
    b = build_frame(123, 1, b"\x11")
    diff = [i for i in range(FRAME_SIZE) if a[i] != b[i]]
    assert diff == [PAYLOAD_OFFSET], f"unexpected extra differences at {diff}"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_payload_must_fit_before_the_frame_boundary():
    assert len(build_frame(1, 0, bytes(FRAME_SIZE - PAYLOAD_OFFSET))) == FRAME_SIZE
    with pytest.raises(ValueError):
        build_frame(1, 0, bytes(FRAME_SIZE - PAYLOAD_OFFSET + 1))


@pytest.mark.parametrize("kwargs", [
    dict(cmd=256), dict(cmd=-1), dict(cmd=1, extra=256), dict(cmd=1, extra=-1),
    dict(cmd=1, value=0x100000000), dict(cmd=1, value=-1),
])
def test_range_validation(kwargs):
    kwargs.setdefault("value", 0)
    with pytest.raises(ValueError):
        build_frame(**kwargs)


def test_parse_rejects_a_bad_magic():
    bad = bytearray(build_frame(1, 1))
    bad[1] = 0x00
    with pytest.raises(ValueError):
        parse_frame(bytes(bad))


def test_parse_rejects_a_truncated_frame():
    with pytest.raises(ValueError):
        parse_frame(b"\x01\xEF")


# --------------------------------------------------------------------------
# the vendor's own HELLO  (뽞.cs:445-446)
# --------------------------------------------------------------------------

def test_hello_matches_the_vendor_call_site_byte_for_byte():
    """뽞.cs:445-446: payload {197,211}, then _A9B7(1, 1, payload, 0)."""
    h = build_hello()
    assert len(h) == FRAME_SIZE
    assert h[:12] == bytes((0x01, 0xEF, 0x69,
                            0x00, 0x00, 0x00, 0x01,   # value = 1, big-endian
                            0x00,                     # extra
                            0x00, 0x00,               # the hole
                            0xC5, 0xD3))              # payload
    assert set(h[12:]) == {0}


def test_hello_matches_the_published_reverse_engineering():
    """Independent public RE of this family records
    HELLO = 01 ef 69 00 00 00 01 00 00 00 c5 d3."""
    published = bytes((0x01, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x01,
                       0x00, 0x00, 0x00, 0xC5, 0xD3))
    h = build_hello()
    assert h[:len(published)] == published, (
        f"our HELLO {h[:12].hex()} != published {published.hex()}")


def test_hello_payload_constant_is_the_vendor_literal():
    assert HELLO_PAYLOAD == bytes((197, 211)) == b"\xC5\xD3"


def test_hello_parses_back():
    f = parse_frame(build_hello())
    assert f.is_hello
    assert f.cmd == SerialCmd.HELLO == 1
    assert f.value == 1
    assert f.extra == 0
    assert f.payload[:2] == HELLO_PAYLOAD


# --------------------------------------------------------------------------
# other recovered commands
# --------------------------------------------------------------------------

def test_brightness_matches_the_call_site():
    """뽞.cs:7921 -> _A9B7(123, 1, {level}, 0).  123 == 0x7B, matching the
    published RE for this family."""
    f = build_brightness(0x50)
    assert SerialCmd.SET_BRIGHTNESS == 123 == 0x7B
    assert f[0] == 0x7B
    assert f[1:3] == b"\xEF\x69"
    assert f[3:7] == b"\x00\x00\x00\x01"
    assert f[PAYLOAD_OFFSET] == 0x50
    assert set(f[PAYLOAD_OFFSET + 1:]) == {0}


def test_rotate_matches_the_call_site():
    """뽞.cs:7943 -> _A9B7(129, 1, {r}, 0)."""
    f = build_rotate(2)
    assert SerialCmd.SET_ROTATE == 129 == 0x81
    assert f[0] == 0x81
    assert f[PAYLOAD_OFFSET] == 2


def test_brightness_and_rotate_range_checks():
    for fn in (build_brightness, build_rotate):
        fn(0)
        fn(255)
        with pytest.raises(ValueError):
            fn(256)
        with pytest.raises(ValueError):
            fn(-1)


def test_published_serial_cmd_names_are_recorded():
    """Values documented by public RE of this family; kept for cross-checking."""
    assert SerialCmd.OPTIONS == 0x7D
    assert SerialCmd.STOP_VIDEO == 0x79
    assert SerialCmd.STOP_MEDIA == 0x96
    assert SerialCmd.RESTART == 0x84
    assert SerialCmd.SCREEN_OFF_ON == 0x83
    assert SerialCmd.FULL_IMAGE == 0xC8
    assert SerialCmd.UPDATE_BITMAP == 0xCC
    assert SerialCmd.QUERY_STATUS == 0xCF
    assert SerialCmd.SEND_PAYLOAD == 0xFF


def test_serial_parameters_match_the_vendor_code():
    assert sp.SERIAL_BAUD == 115200        # 뽞.cs:8359, 됒.cs:1512
    assert sp.SERIAL_DATABITS == 8
    assert sp.SERIAL_PARITY == "N"         # Parity.None
    assert sp.SERIAL_STOPBITS == 1         # StopBits.One
    assert sp.SERIAL_DTR is True           # DtrEnable = true
    assert sp.SERIAL_RTS is True           # RtsEnable = true


# --------------------------------------------------------------------------
# reply decoding  (no hardware -- synthetic inputs only)
# --------------------------------------------------------------------------

def test_decode_reply_extracts_the_identity_and_version():
    payload = b"chs_65inch.dev1_rom1.88" + b"\x00" * 400
    d = decode_reply(payload)
    assert d["identity"] == "chs_65inch.dev1_rom1.88"
    assert d["version"] == pytest.approx(1.88)


def test_decode_reply_of_an_empty_response():
    d = decode_reply(b"")
    assert d["raw_len"] == 0
    assert d["identity"] is None and d["version"] is None


def test_decode_reply_tolerates_binary_noise():
    d = decode_reply(b"\x01\x02\x00abcd\xff\xfe" + b"\x00" * 300)
    assert d["identity"] == "abcd"
    assert d["version"] is None


def test_decode_reply_counts_frames():
    assert decode_reply(bytes(250))["frames"] == 1
    assert decode_reply(bytes(251))["frames"] == 2
    assert decode_reply(bytes(1024))["frames"] == 5


def test_decode_reply_handles_a_bare_version_string():
    d = decode_reply(b"chs_4inch.dev1_rom1.91" + bytes(230))
    assert d["version"] == pytest.approx(1.91)
    assert d["identity"].startswith("chs_4inch")


# --------------------------------------------------------------------------
# this module must not be confused with the 0x1CBE/DES protocol
# --------------------------------------------------------------------------

def test_serial_frames_are_not_des_framed():
    """A serial frame must never look like a 0x1CBE 512-byte DES packet."""
    import protocol
    h = build_hello()
    assert len(h) == 250 != protocol.PACKET_SIZE
    assert not h.endswith(protocol.TRAILER)          # no A1 1A trailer
    assert h[1:3] == MAGIC                           # plaintext magic, not ciphertext
