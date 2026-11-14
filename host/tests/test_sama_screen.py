"""Tests for the SamaScreen API.

Everything here runs against a mock transport -- no hardware, no COM port.
The point is to pin the safety contract: the verified feature works out of the
box, the unverified ones refuse with a reason, and nothing can reach firmware.
"""

import pytest
from PIL import Image

import image_encoder as ie
import serial_proto as sp
from device import MockTransport
from encoder import SAFE_PAD, safe_area
from safety import Safety, WriteClass
from sama_screen import (PANEL_PID, PANEL_VID, ScreenError, SamaScreen,
                         UnverifiedOperation, find_panel_port)


def make_screen(**kw):
    """A screen wired to a recording mock transport."""
    t = MockTransport()
    t.writes = []                      # ensure a fresh list
    s = SamaScreen(transport=t, handshake=False, **kw)
    return s, t


# --------------------------------------------------------------------------
# construction / lifecycle
# --------------------------------------------------------------------------

def test_panel_identity_constants_are_the_confirmed_ones():
    assert PANEL_VID == 0x1D6B
    assert PANEL_PID == 0xA065


def test_image_is_authorized_by_default():
    s, _ = make_screen()
    assert s.safety.allows(WriteClass.IMAGE)
    assert not s.safety.allows(WriteClass.BRIGHTNESS)
    assert not s.safety.allows(WriteClass.FIRMWARE)


def test_firmware_can_never_be_enabled_through_the_api():
    """FORBIDDEN_FOREVER must survive even an explicit attempt."""
    with pytest.raises(Exception):
        SamaScreen(transport=MockTransport(), allow=(WriteClass.FIRMWARE,))


def test_not_connected_raises():
    s, _ = make_screen()
    with pytest.raises(Exception):
        s.show_image(Image.new("RGB", (8, 8)))


def test_context_manager_connects_and_disconnects():
    with SamaScreen(transport=MockTransport(), handshake=False) as s:
        assert s.is_connected
    assert not s.is_connected


def test_repr_is_informative():
    s, _ = make_screen()
    s.connect()
    r = repr(s)
    assert "65inch" in r and "connected" in r


def test_mock_needs_no_port():
    s, _ = make_screen()
    s.connect()
    assert s.is_mock


def test_connect_fails_cleanly_when_no_panel_is_present(monkeypatch):
    import sama_screen
    monkeypatch.setattr(sama_screen, "find_panel_port",
                        lambda: (None, "no COM port found for 1D6B:A065"))
    s = SamaScreen(port=None)
    with pytest.raises(ScreenError) as ei:
        s.connect()
    assert "1D6B:A065" in str(ei.value)


def test_find_panel_port_reports_something_useful():
    port, why = find_panel_port()
    assert isinstance(why, str) and why
    if port is None:
        assert "1D6B:A065" in why


# --------------------------------------------------------------------------
# show_image -- the verified feature
# --------------------------------------------------------------------------

def test_show_image_sends_exactly_one_full_frame():
    s, t = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (64, 64), (10, 20, 30)))
    blob = b"".join(t.writes)
    assert blob[:3] == b"\xC8\xEF\x69"                  # cmd 200 + magic
    assert len(blob) == 250 + ie.chunked_wire_size(720 * 1568 * 4)
    assert blob[3:7] == (720 * 1568 * 4).to_bytes(4, "big")


def _pixels_of(transport):
    """Reassemble the pixel stream from the recorded writes and index it.

    IMPORTANT: the payload is NOT a contiguous byte stream on the wire.  Each
    250-byte frame carries **249 payload bytes plus one zero pad**
    (`됒.m_ꔂ = 249`, `됒.m_ꗳ = 250`, `됒.cs:2855-2960`), so the pads must be
    stripped before the bytes can be read as 4-bytes-per-pixel BGRA.  Getting this
    wrong shifts every index after the first frame -- which is exactly how this
    helper was originally written.

    Also note the pixel order is BGRA, so a black pixel is (0, 0, 0, 255): the
    alpha byte is 0xFF and must not be mistaken for content.
    """
    blob = b"".join(transport.writes)
    if blob[:3] != b"\xC8\xEF\x69":
        raise AssertionError("stream does not start with the cmd 200 frame")
    # the command frame carries the exact payload length in its `value` field
    declared = int.from_bytes(blob[3:7], "big")
    body = blob[250:]
    if len(body) % 250:
        raise AssertionError(f"payload tail is not frame aligned: {len(body)}")

    frames = [body[i:i + 250] for i in range(0, len(body), 250)]
    for n, f in enumerate(frames[:-1]):
        if f[249] != 0:
            raise AssertionError(f"frame {n} pad byte is {f[249]}, expected 0")
    px = b"".join(f[:249] for f in frames)

    # the final frame is zero padded, so trim to what the header declared
    if len(px) < declared:
        raise AssertionError(f"declared {declared} payload bytes but only "
                             f"{len(px)} were sent")
    if set(px[declared:]) != {0}:
        raise AssertionError("bytes beyond the declared length are not padding")
    px = px[:declared]

    def at(x, y):
        off = y * 720 * 4 + x * 4
        b, g, r, a = px[off:off + 4]
        return (r, g, b, a)

    return at, len(px)


def test_show_image_defaults_to_fit_safe():
    """Content must not be drawn into the measured cropped margins."""
    s, t = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (1000, 100), (255, 255, 255)))
    at, total = _pixels_of(t)
    assert total == 720 * 1568 * 4

    l, top, r, b = safe_area(s.panel, SAFE_PAD)
    for y in (0, 10, 21, 22, 29, 1567, 1540):
        assert at(360, y)[:3] == (0, 0, 0), f"row {y} is not background"
    # the white content sits inside the safe area, not at the edges
    assert at(360, s.panel.height // 2)[:3] == (255, 255, 255)
    assert top >= 22 and s.panel.height - 1 - b >= 42


def test_show_image_can_disable_fit_safe():
    s, t = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (720, 1568), (255, 0, 0)), fit_safe=False)
    pixels = b"".join(t.writes)[250:]
    assert pixels[0:4] == bytes((0, 0, 255, 255))       # BGRA red at row 0


def test_show_image_accepts_a_path(tmp_path):
    p = tmp_path / "t.png"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(p)
    s, t = make_screen()
    s.connect()
    s.show_image(str(p))
    assert len(b"".join(t.writes)) == 250 + ie.chunked_wire_size(720 * 1568 * 4)


def test_show_image_rejects_a_non_image():
    s, _ = make_screen()
    s.connect()
    with pytest.raises(ScreenError):
        s.show_image(object())


def test_show_image_updates_stats():
    s, _ = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (16, 16)))
    st = s.stats
    assert st.images == 1
    assert st.frames == 1 + 18136
    assert st.bytes_sent == 250 + 18136 * 250
    assert "images=1" in st.summary()


def test_show_image_batching_is_transparent():
    """batch changes the number of write() calls, never the bytes."""
    a, ta = make_screen(batch=1)
    b, tb = make_screen(batch=256)
    for s in (a, b):
        s.connect()
        s.show_image(Image.new("RGB", (16, 16), (7, 7, 7)))
    assert b"".join(ta.writes) == b"".join(tb.writes)
    assert len(ta.writes) == 18137
    assert len(tb.writes) == 71


def test_show_image_fit_safe_uses_the_measured_geometry():
    """The measured crop margins must stay background even with fit_safe on."""
    s, t = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (1920, 1080)), fit_safe=True)
    at, _ = _pixels_of(t)

    # the measured top 22 and bottom 42 rows are never written into
    for y in list(range(22)) + list(range(1568 - 42, 1568)):
        assert at(0, y)[:3] == (0, 0, 0), f"content leaked into margin row {y}"
        assert at(719, y)[:3] == (0, 0, 0), f"content leaked into margin row {y}"
    # and the safe padding pushes it further in still
    for y in range(30):
        assert at(360, y)[:3] == (0, 0, 0)


def test_fit_safe_margins_are_background_not_alpha():
    """Regression: a black pixel is (0,0,0,255) -- alpha is not content."""
    s, t = make_screen()
    s.connect()
    s.show_image(Image.new("RGB", (100, 100), (0, 0, 0)), fit_safe=True)
    at, _ = _pixels_of(t)
    assert at(360, 0) == (0, 0, 0, 255)
    assert at(360, 5)[:3] == (0, 0, 0)


# --------------------------------------------------------------------------
# gated features
# --------------------------------------------------------------------------

def test_brightness_refuses_by_default_with_a_reason():
    s, _ = make_screen()
    s.connect()
    with pytest.raises(UnverifiedOperation) as ei:
        s.set_brightness(80)
    msg = str(ei.value)
    assert "cmd 123" in msg and "125" in msg and "app.cfg" in msg


def test_brightness_works_once_enabled():
    s, t = make_screen(allow=(WriteClass.BRIGHTNESS,))
    s.connect()
    s.set_brightness(80)
    assert t.writes[-1][:3] == bytes((123, 0xEF, 0x69))


def test_brightness_validates_range():
    s, _ = make_screen(allow=(WriteClass.BRIGHTNESS,))
    s.connect()
    for bad in (-1, 256):
        with pytest.raises(ValueError):
            s.set_brightness(bad)


def test_rotation_refuses_by_default():
    s, _ = make_screen()
    s.connect()
    with pytest.raises(UnverifiedOperation) as ei:
        s.set_rotation(0)
    assert "129" in str(ei.value)


def test_rotation_works_once_enabled():
    s, t = make_screen(allow=(WriteClass.ROTATION,))
    s.connect()
    s.set_rotation(1)
    assert t.writes[-1][:3] == bytes((129, 0xEF, 0x69))


def test_update_region_refuses_until_hardware_verified():
    s, _ = make_screen(allow=(WriteClass.REGION,))
    s.connect()
    with pytest.raises((UnverifiedOperation, Exception)) as ei:
        s.update_region(Image.new("RGB", (8, 8)), 0, 0)
    msg = str(ei.value)
    assert "cmd 204" in msg.lower()
    assert "never been tested on hardware" in msg.lower()


def test_update_region_never_writes_anything():
    s, t = make_screen(allow=(WriteClass.REGION,))
    s.connect()
    before = len(t.writes)
    with pytest.raises(Exception):
        s.update_region(Image.new("RGB", (8, 8)), 0, 0)
    assert len(t.writes) == before


# --------------------------------------------------------------------------
# ping / read-only surface
# --------------------------------------------------------------------------

def test_ping_on_a_mock_returns_the_stored_identity():
    s, _ = make_screen()
    s.connect()
    assert s.ping() == s.identity


def test_safe_area_is_the_confirmed_one():
    s, _ = make_screen()
    l, t, r, b = s.safe_area()
    assert t == 22 + SAFE_PAD
    assert b == 1568 - 1 - 42 - SAFE_PAD
    assert l == SAFE_PAD


def test_no_api_surface_can_reach_firmware():
    """Nothing on the class mentions a firmware/erase/flash write class."""
    import inspect
    import sama_screen
    src = inspect.getsource(sama_screen)
    for call in ("authorize(WriteClass.FIRMWARE",
                 "WriteClass.UPLOAD",
                 "WriteClass.CONFIG"):
        assert call not in src, f"unexpected reachable call: {call}"


def test_hello_frame_used_by_connect_is_the_verified_one():
    """Guard against the handshake silently drifting from 뽞.cs:445-446."""
    h = sp.build_hello()
    assert h[:12] == bytes((0x01, 0xEF, 0x69, 0, 0, 0, 0x01, 0, 0, 0, 0xC5, 0xD3))
    assert len(h) == 250


def test_safety_gate_still_refuses_unauthorized_image_write():
    """Even with a transport injected, show_image must pass the gate."""
    t = MockTransport()
    s = SamaScreen(transport=t, handshake=False)
    s.connect()
    s.safety.revoke_all()
    with pytest.raises(Exception):
        s.show_image(Image.new("RGB", (8, 8)))
    assert s.safety.is_read_only
