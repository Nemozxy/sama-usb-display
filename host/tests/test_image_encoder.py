"""Tests for the serial-family image encoder and framing.

Golden data comes from TWO independent sources wherever possible:

1. the decompiled vendor code (cited per test), and
2. the published reverse engineering of this family.

No device is touched.  Acceptance by real hardware is NOT claimed.
"""

import pytest
from PIL import Image

import image_encoder as ie
import serial_proto as sp
from image_encoder import (BYTES_PER_PIXEL, CHUNK_FRAME, CHUNK_PAYLOAD,
                           ImageCmd, RLE_RUN_BYTES, build_differ_init,
                           build_full_image, build_mask_payload, chunked_wire_size,
                           decode_rle_mask, encode_pixels, expected_pixel_bytes,
                           guard_family, iter_chunks, plan_image, rle_mask,
                           send_plan, to_bgra)


def solid(w, h, rgb=(10, 20, 30)):
    return Image.new("RGB", (w, h), rgb)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

def test_chunk_constants_match_the_vendor_code():
    """됒.cs:3219-3220 -- m_ꔂ = 249, m_ꗳ = 250."""
    assert CHUNK_PAYLOAD == 249
    assert CHUNK_FRAME == 250
    assert sp.FRAME_SIZE == 250


def test_command_numbers_match_the_vendor_call_sites():
    assert ImageCmd.FULL_IMAGE == 200 == 0xC8
    assert ImageCmd.DIFFER_INIT == 202 == 0xCA
    assert ImageCmd.SET_POS_INFO == 208 == 0xD0
    assert ImageCmd.UPDATE_BITMAP == 204 == 0xCC


def test_pixel_format_is_four_bytes_per_pixel():
    """Monitor.cs:5226 passes 4; 쐓.ꮙ(4) selects the bpp."""
    assert BYTES_PER_PIXEL == 4
    assert expected_pixel_bytes(720, 1568) == 720 * 1568 * 4 == 4_515_840


# --------------------------------------------------------------------------
# chunking  (됒.cs:2855-2960)
# --------------------------------------------------------------------------

def test_every_chunk_is_a_full_250_byte_frame():
    for n in (1, 248, 249, 250, 498, 1000):
        frames = iter_chunks(bytes(n))
        assert all(len(f) == CHUNK_FRAME for f in frames), n
        assert len(frames) == (n + CHUNK_PAYLOAD - 1) // CHUNK_PAYLOAD


def test_chunk_payload_is_verbatim_and_in_order():
    data = bytes((i * 7) & 0xFF for i in range(1000))
    frames = iter_chunks(data)
    joined = b"".join(f[:CHUNK_PAYLOAD] for f in frames)
    assert joined[:len(data)] == data            # last frame is zero padded


def test_last_chunk_is_zero_padded():
    data = bytes(range(200))
    frames = iter_chunks(data)
    assert len(frames) == 1
    assert frames[0][:200] == data
    assert set(frames[0][200:]) == {0}


def test_exact_multiple_of_249_does_not_emit_an_empty_trailing_frame():
    data = bytes(249 * 3)
    assert len(iter_chunks(data)) == 3


def test_boundary_at_249_and_250():
    """The 249/250 boundary is the single most likely off-by-one."""
    assert len(iter_chunks(bytes(249))) == 1
    assert len(iter_chunks(bytes(250))) == 2
    assert iter_chunks(bytes(249))[0][248] == 0
    two = iter_chunks(bytes(250))
    assert two[0][:249] == bytes(249)
    assert two[1][0] == 0 and set(two[1][1:]) == {0}


def test_chunked_wire_size_accounts_for_padding():
    assert chunked_wire_size(0) == 0
    assert chunked_wire_size(249) == 250
    assert chunked_wire_size(250) == 500
    assert chunked_wire_size(4_515_840) == ((4_515_840 + 248) // 249) * 250


def test_iter_chunks_rejects_an_impossible_payload_size():
    with pytest.raises(ValueError):
        iter_chunks(b"x", payload=0)
    with pytest.raises(ValueError):
        iter_chunks(b"x", payload=251)


# --------------------------------------------------------------------------
# full-frame sequence  (뽞.cs:6990-7040)
# --------------------------------------------------------------------------

def test_full_image_command_frame_is_byte_exact():
    pixels = bytes(1000)
    plan = build_full_image(pixels)
    f = plan.command_frame
    assert len(f) == 250
    assert f[0] == 200
    assert f[1:3] == b"\xEF\x69"
    assert f[3:7] == (1000).to_bytes(4, "big")     # value = pixel byte count
    assert f[7] == 0
    assert f[8] == 0 and f[9] == 0                 # the [8..9] hole
    assert set(f[10:]) == {0}                      # no payload on the command frame


def test_full_image_plan_shape():
    pixels = bytes(600)
    plan = build_full_image(pixels)
    assert plan.mode == "full"
    assert plan.pixel_bytes == 600
    assert len(plan.data_frames) == 3               # 249 + 249 + 102
    assert plan.trailing_frames == []
    assert plan.wire_bytes == 250 + 3 * 250 == 1000


def test_full_image_frames_reassemble_to_the_original_pixels():
    pixels = bytes((i * 13) & 0xFF for i in range(4321))
    plan = build_full_image(pixels)
    got = b"".join(f[:CHUNK_PAYLOAD] for f in plan.data_frames)[:len(pixels)]
    assert got == pixels


def test_full_image_of_the_real_panel_size_is_the_expected_size():
    """720x1568 at 4 B/px -- the geometry from the vendor table entry Dev_65inch."""
    n = expected_pixel_bytes(720, 1568)
    plan = build_full_image(bytes(0) * 0 + b"\x00" * n)
    assert plan.pixel_bytes == 4_515_840
    assert len(plan.data_frames) == 18136          # ceil(4515840 / 249)
    assert plan.wire_bytes == 250 + 18136 * 250


# --------------------------------------------------------------------------
# differ-init sequence  (뽞.cs:7041-7054, 6574-6658)
# --------------------------------------------------------------------------

def test_differ_init_uses_cmd_202_then_208():
    pixels = bytes(500)
    mask = build_mask_payload(bytes(RLE_RUN_BYTES))
    plan = build_differ_init(pixels, mask)
    assert plan.mode == "differ-init"
    assert plan.command_frame[0] == 202
    assert plan.command_frame[3:7] == (500).to_bytes(4, "big")
    assert plan.trailing_frames[0][0] == 208
    assert plan.trailing_frames[0][3:7] == len(mask).to_bytes(4, "big")


def test_mask_payload_appends_the_ef69_magic():
    """뽞.cs:6642-6645 -- array[A_]=0xEF; array[A_+1]=0x69."""
    mask = bytes(RLE_RUN_BYTES * 3)
    payload = build_mask_payload(mask)
    assert payload == mask + b"\xEF\x69"
    assert payload[-2:] == sp.MAGIC


# --------------------------------------------------------------------------
# RLE mask  (깬.cs:4574-4703)
# --------------------------------------------------------------------------

def _bgra(pixels):
    """pixels: list of (b,g,r,count_high_nibble_byte)."""
    out = bytearray()
    for b, g, r, a in pixels:
        out += bytes((b, g, r, a))
    return bytes(out)


def test_rle_emits_six_bytes_per_run():
    src = _bgra([(0x11, 0x22, 0x33, 0x10)])       # count = 1
    m = rle_mask(src)
    assert len(m) == RLE_RUN_BYTES


def test_rle_golden_bytes():
    """Hand-computed from 깬.cs:4677-4683.

    pixel 5, B=0xAB G=0xCD R=0xEF, count nibble = 3 (0x30 -> count 3)
        index 5 -> 00 00 05
        B & 0xFC = 0xA8 ; count>>2 = 0 -> 0xA8
        G & 0xFC = 0xCC ; count&3  = 3 -> 0xCF
        R       = 0xEF
    """
    src = bytearray(6 * 4)
    src[5 * 4 + 0] = 0xAB
    src[5 * 4 + 1] = 0xCD
    src[5 * 4 + 2] = 0xEF
    src[5 * 4 + 3] = 0x30                          # count nibble 3
    m = rle_mask(bytes(src))
    assert m == bytes((0x00, 0x00, 0x05, 0xA8, 0xCF, 0xEF))


def test_rle_index_is_24_bit_big_endian():
    src = bytearray(0x12345 * 4 + 4)
    src[0x12345 * 4 + 3] = 0x10
    m = rle_mask(bytes(src))
    assert m[0:3] == bytes((0x01, 0x23, 0x45))
    assert decode_rle_mask(m)[0]["index"] == 0x12345


def test_rle_skips_zero_count_pixels():
    src = _bgra([(1, 2, 3, 0x00), (4, 5, 6, 0x10), (7, 8, 9, 0x00)])
    m = rle_mask(src)
    runs = decode_rle_mask(m)
    assert len(runs) == 1
    assert runs[0]["index"] == 1


def test_rle_count_is_the_high_nibble_and_round_trips():
    for count in range(1, 16):
        src = _bgra([(0x00, 0x00, 0x00, count << 4)])
        runs = decode_rle_mask(rle_mask(src))
        assert runs[0]["count"] == count


def test_rle_count_round_trips_through_the_interleaved_bits():
    """count 2 MSBs ride in B's low bits, 2 LSBs in G's low bits."""
    for count in (1, 4, 5, 15):
        src = _bgra([(0xFC, 0xFC, 0x00, count << 4)])
        runs = decode_rle_mask(rle_mask(src))
        assert runs[0]["count"] == count
        assert runs[0]["b"] == 0xFC and runs[0]["g"] == 0xFC


def test_rle_colour_channels_are_quantised_to_6_bits():
    """The low 2 bits of B and G are consumed by the run count -> lost."""
    src = _bgra([(0xFF, 0xFF, 0xFF, 0x10)])
    run = decode_rle_mask(rle_mask(src))[0]
    assert run["b"] == 0xFC and run["g"] == 0xFC      # NOT 0xFF
    assert run["r"] == 0xFF                            # R keeps all 8 bits


def test_rle_rejects_a_run_byte_over_250():
    """깬.cs:4611 -- the emitter requires the 4th byte to be <= 250."""
    src = _bgra([(1, 2, 3, 0xFF)])                     # 255 > 250
    assert rle_mask(src) == b""


def test_rle_explicit_counts_override_the_alpha_channel():
    src = _bgra([(0x10, 0x20, 0x30, 0x00)] * 4)
    m = rle_mask(src, counts=bytes((3, 0, 0, 1)))
    runs = decode_rle_mask(m)
    assert [r["index"] for r in runs] == [0, 3]
    assert runs[0]["count"] == 3 and runs[1]["count"] == 1


def test_rle_rejects_malformed_input():
    with pytest.raises(ValueError):
        rle_mask(bytes(5))                              # not a multiple of 4
    with pytest.raises(ValueError):
        rle_mask(bytes(8), counts=bytes(5))             # wrong count array size
    with pytest.raises(ValueError):
        decode_rle_mask(bytes(5))                       # not a multiple of 6


def test_rle_mask_round_trips_a_realistic_frame():
    w, h = 8, 4
    src = bytearray(w * h * 4)
    for p in range(w * h):
        src[p * 4 + 0] = (p * 3) & 0xFF
        src[p * 4 + 1] = (p * 5) & 0xFF
        src[p * 4 + 2] = (p * 7) & 0xFF
        src[p * 4 + 3] = 0x10 if p % 3 == 0 else 0x00
    runs = decode_rle_mask(rle_mask(bytes(src)))
    assert [r["index"] for r in runs] == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]


# --------------------------------------------------------------------------
# pixel conversion / channel order
# --------------------------------------------------------------------------

def test_bgra_channel_order():
    """A 32bppArgb Bitmap stores B,G,R,A -- verified with a pure-red pixel."""
    img = Image.new("RGB", (1, 1), (255, 0, 0))
    assert to_bgra(img) == bytes((0x00, 0x00, 0xFF, 0xFF))


def test_bgra_all_primaries():
    assert to_bgra(Image.new("RGB", (1, 1), (0, 255, 0))) == bytes((0, 255, 0, 255))
    assert to_bgra(Image.new("RGB", (1, 1), (0, 0, 255))) == bytes((255, 0, 0, 255))
    assert to_bgra(Image.new("RGB", (1, 1), (0, 0, 0))) == bytes((0, 0, 0, 255))


def test_encode_pixels_size_matches_the_bitmap():
    img = solid(37, 11)
    assert len(encode_pixels(img)) == 37 * 11 * 4


def test_encode_pixels_rejects_an_unused_bpp():
    """Only bpp=4 appears in any recovered call site."""
    for bad in (1, 2, 3, 5):
        with pytest.raises(ValueError):
            encode_pixels(solid(2, 2), bpp=bad)


# --------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------

def test_plan_full_image_end_to_end():
    plan = plan_image(solid(16, 8, (255, 0, 0)), mode="full")
    assert plan.mode == "full"
    assert plan.pixel_bytes == 16 * 8 * 4 == 512
    assert plan.command_frame[0] == 200
    assert plan.command_frame[3:7] == (512).to_bytes(4, "big")
    assert len(plan.data_frames) == 3               # 249 + 249 + 14


def test_plan_differ_init_end_to_end():
    plan = plan_image(solid(16, 8), mode="differ-init")
    assert plan.mode == "differ-init"
    assert plan.command_frame[0] == 202
    assert plan.trailing_frames[0][0] == 208
    # the default mask is one run per pixel: 16*8*6 bytes, plus the 2-byte magic
    expected_mask = 16 * 8 * RLE_RUN_BYTES + 2
    assert plan.trailing_frames[0][3:7] == expected_mask.to_bytes(4, "big")


def test_plain_alpha_of_255_cannot_be_used_as_a_mask_source():
    """깬.cs:4611 requires the run byte to be <= 250; a normal alpha of 255 is not.

    This is why the vendor derives the mask from a frame difference rather than
    from the frame itself -- and why `plan_image` supplies explicit counts.
    """
    src = _bgra([(1, 2, 3, 0xFF)])           # alpha 255 > 250
    assert rle_mask(src) == b""
    src2 = _bgra([(1, 2, 3, 0xF0)])          # count 15 -> 240 <= 250
    assert len(rle_mask(src2)) == RLE_RUN_BYTES


def test_plan_differ_init_rejects_a_wrong_sized_counts_array():
    with pytest.raises(ValueError):
        plan_image(solid(4, 4), mode="differ-init", counts=bytes(3))


def test_plan_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        plan_image(solid(4, 4), mode="bogus")


def test_plan_described_is_human_readable():
    txt = plan_image(solid(4, 4)).described()
    assert "mode=full" in txt and "pixel_bytes=" in txt and "wire_bytes=" in txt


# --------------------------------------------------------------------------
# safety / transport
# --------------------------------------------------------------------------

def test_send_plan_writes_every_frame_in_order():
    from device import MockTransport
    plan = build_full_image(bytes(300))
    t = MockTransport()
    n = send_plan(t, plan)
    assert n == 1 + len(plan.data_frames) == 3
    assert [len(w) for w in t.writes] == [250, 250, 250]
    assert t.writes[0][0] == 200


def test_send_plan_requires_image_authorization():
    from device import MockTransport
    from safety import Safety, WriteNotAuthorized
    s = Safety()                                     # read-only
    with pytest.raises(WriteNotAuthorized):
        send_plan(MockTransport(), build_full_image(bytes(300)), safety=s)
    assert s.is_read_only


def test_send_plan_works_once_image_is_authorized():
    from device import MockTransport
    from safety import Safety, WriteClass
    s = Safety()
    s.authorize(WriteClass.IMAGE)
    t = MockTransport()
    send_plan(t, build_full_image(bytes(10)), safety=s)
    assert len(t.writes) == 2


def test_guard_family_blocks_the_wrong_transport():
    guard_family("serial COM3")                       # ok
    guard_family("mock")                              # ok
    with pytest.raises(ValueError):
        guard_family("libusb/WinUSB 1CBE:A062")


def test_serial_frames_must_not_look_like_des_frames():
    import protocol
    plan = build_full_image(bytes(400))
    for f in plan.all_frames():
        assert len(f) == 250, "serial frames are 250 bytes, not 512"
        assert not f.endswith(protocol.TRAILER)
        assert len(f) != protocol.PACKET_SIZE
    assert plan.command_frame[1:3] == b"\xEF\x69"
