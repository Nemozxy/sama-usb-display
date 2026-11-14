"""SamaScreen -- a minimal, safe Python API for the 6.5-inch SAMA USB panel.

    from sama_screen import SamaScreen

    with SamaScreen() as screen:
        print(screen.identity)          # chs_65inch.dev1_rom1.91
        screen.show_image("test.png")

What this wraps
---------------
The CDC-ACM serial family (CH552 "rev-C"), NOT the 0x1CBE/DES family.  The panel
in hand enumerates as ``1D6B:A065`` and self-reports ``chs_65inch.dev1_rom1.91``.
The full protocol is in `serial_proto.py` and `image_encoder.py`; this module is
just a friendly, gated front door.

Safety model
------------
* ``show_image()`` uses cmd 200 (full frame).  That is **hardware-verified and
  non-persistent**: it writes no configuration, no device storage, no firmware.
  It is therefore enabled by default.
* ``set_brightness()`` / ``set_rotation()`` / ``update_region()`` are **NOT
  hardware-verified** and one of them may touch persistent state, so they raise
  unless explicitly enabled at construction.  See each docstring.
* Nothing here can flash, erase or rebless the device.  Firmware update is in
  `safety.FORBIDDEN_FOREVER` and is unreachable from this API.

Auto-detection
--------------
``SamaScreen()`` with no arguments finds the panel by USB vendor/product id, so
the COM port never has to be hard-coded (the port number is dynamic on Windows).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import image_encoder as ie
import serial_proto as sp
from device import MockTransport, SerialTransport, com_ports_for
from encoder import SAFE_PAD, get_panel
from encoder import fit_safe as fit_into_safe_area
from safety import ForbiddenOperation, Safety, WriteClass, WriteNotAuthorized

log = logging.getLogger("sama.screen")

#: The panel's Linux-gadget identity.  `1D6B` is the Linux Foundation vendor id
#: and `iManufacturer` reads "Linux 5.4.61 with sunxi_usb_udc".
PANEL_VID = 0x1D6B
PANEL_PID = 0xA065

#: The CH552 wake-MCU identity.  Deliberately NOT used for image data: it is a
#: Full-Speed 12 Mbit/s link with 64-byte bulk packets, ~40x slower than COM3.
WAKE_VID = 0x1A86
WAKE_PID = 0xCA65


class ScreenError(RuntimeError):
    """Anything that stops the screen from doing what was asked."""


class NotConnected(ScreenError):
    pass


class UnverifiedOperation(ScreenError):
    """Raised when a not-yet-hardware-verified feature is used.

    Carries an explanation so the caller learns *why* rather than just getting a
    refusal.
    """


@dataclass
class ScreenStats:
    frames: int = 0
    bytes_sent: int = 0
    images: int = 0
    connect_time: float = 0.0
    last_image_seconds: float = 0.0
    history: list[tuple[str, float]] = field(default_factory=list)

    def record_image(self, seconds: float) -> None:
        self.images += 1
        self.last_image_seconds = seconds

    def summary(self) -> str:
        mib = self.bytes_sent / 1048576
        return (f"images={self.images} frames={self.frames} "
                f"bytes={self.bytes_sent:,} ({mib:.2f} MiB) "
                f"last={self.last_image_seconds:.2f}s")


def find_panel_port() -> tuple[str | None, str]:
    """Locate the panel's COM port.  Returns (port, reason)."""
    ports = com_ports_for(PANEL_VID, PANEL_PID)
    if not ports:
        return None, (f"no COM port found for "
                      f"{PANEL_VID:04X}:{PANEL_PID:04X}. Is the panel plugged in? "
                      f"(the port number is dynamic, which is why this is looked up "
                      f"rather than hard-coded)")
    if len(ports) > 1:
        names = ", ".join(p.device for p in ports)
        return ports[0].device, f"multiple candidates ({names}); using the first"
    return ports[0].device, f"found on {ports[0].device}"


class SamaScreen:
    """Control one 6.5-inch SAMA / TURZX serial panel.

    Parameters
    ----------
    port
        COM port.  ``None`` (default) auto-detects.
    panel
        Geometry key; ``"65inch"`` is the unit in hand (720x1568).
    allow
        Extra write classes to enable.  ``IMAGE`` is always enabled.  Pass e.g.
        ``allow=(WriteClass.BRIGHTNESS,)`` to unlock ``set_brightness()``.
    fit_safe
        Scale images into the measured safe area (rows 22..1525) by default.
    batch, pause_ms
        Write batching and inter-frame delay.  Defaults match the measured best
        configuration: 256 frames per write and no artificial delay, which took a
        full 720x1568 frame from 35.7 s down to 0.23 s.
    handshake
        Send the vendor HELLO on connect to confirm the device and read its
        identity.  One 250-byte frame; this is the vendor's own first step.
    """

    def __init__(self, port: str | None = None, panel: str = "65inch",
                 allow: tuple = (), fit_safe_default: bool = True,
                 batch: int = 256, pause_ms: float = 0.0,
                 handshake: bool = True, write_timeout: float = 5.0,
                 read_timeout: float = 0.3, transport=None,
                 transport_factory=None) -> None:
        self.panel = get_panel(panel)
        self.safety = Safety()
        self.safety.authorize(WriteClass.IMAGE)
        for cls in allow:
            self.safety.authorize(cls if isinstance(cls, WriteClass)
                                  else WriteClass(str(cls)))

        self.port = port
        self.batch = max(1, int(batch))
        self.pause_ms = max(0.0, float(pause_ms))
        self.handshake = handshake
        self.fit_safe_default = fit_safe_default
        self.write_timeout = write_timeout
        self.read_timeout = read_timeout

        self._transport = transport
        self._factory = transport_factory or self._default_factory
        self._owns_transport = transport is None
        self.identity: str | None = None
        self.rom_version: float | None = None
        self.stats = ScreenStats()
        self._connected = False

    # -- transport ---------------------------------------------------------

    def _default_factory(self, port: str):
        return SerialTransport(port, baudrate=sp.SERIAL_BAUD,
                               read_timeout=self.read_timeout,
                               write_timeout=self.write_timeout)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_mock(self) -> bool:
        return isinstance(self._transport, MockTransport)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> "SamaScreen":
        """Open the port and (by default) run the HELLO handshake."""
        t0 = time.perf_counter()

        if self._transport is None:
            port = self.port
            if port is None:
                port, why = find_panel_port()
                log.info("auto-detect: %s", why)
                if port is None:
                    raise ScreenError(why)
                self.port = port
            self._transport = self._factory(port)

        self._connected = True
        self.stats.connect_time = time.perf_counter() - t0

        if self.handshake and not self.is_mock:
            self._hello()
        log.info("connected to %s (%s) in %.2fs", self.port, self.panel.key,
                 self.stats.connect_time)
        return self

    def _hello(self) -> None:
        """Send the vendor HELLO and decode the reply.

        This is `뽞.cs:445-446` verbatim and is byte-for-byte identical to the
        published HELLO for this family.  It writes nothing.
        """
        self._transport.write(sp.build_hello())
        buf = bytearray()
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 3.0:
            chunk = self._transport.read(2048, timeout=self.read_timeout)
            if chunk:
                buf += chunk
                if len(buf) >= sp.REPLY_SIZE_HINT:
                    break
        if not buf:
            log.warning("HELLO produced no reply; the panel may need a power cycle")
            return
        d = sp.decode_reply(bytes(buf))
        self.identity = d.get("identity")
        self.rom_version = d.get("version")
        if not self.identity:
            log.warning("HELLO reply could not be decoded: %r", bytes(buf)[:64])
        else:
            log.info("panel says %r (rom %.2f)", self.identity,
                     self.rom_version or 0.0)

    def disconnect(self) -> None:
        if self._transport is not None and self._owns_transport:
            try:
                self._transport.close()
            except Exception as exc:                          # noqa: BLE001
                log.debug("close failed: %s", exc)
        self._connected = False

    def __enter__(self) -> "SamaScreen":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return (f"<SamaScreen {self.panel.key} {self.port or 'auto'} {state} "
                f"identity={self.identity!r}>")

    # -- the verified feature ---------------------------------------------

    def show_image(self, image, fit_safe: bool | None = None,
                   batch: int | None = None, pause_ms: float | None = None,
                   on_progress=None) -> ScreenStats:
        """Display a full frame.  **Hardware-verified, non-persistent.**

        `image` may be a path or an already-open PIL Image.  A full 720x1568
        frame is 4,515,840 bytes of raw BGRA and takes about 0.23 s on COM3.

        Uses cmd 200, which writes no configuration, no device storage and no
        firmware.
        """
        self._require_connected()
        from PIL import Image

        if isinstance(image, (str, os.PathLike)):
            src = Image.open(image)
        else:
            src = image
        if not hasattr(src, "convert"):
            raise ScreenError(f"unsupported image type: {type(image).__name__}")

        if (self.fit_safe_default if fit_safe is None else fit_safe):
            src = fit_into_safe_area(src, self.panel)

        # Fast path: `frames=False` skips building ~18,000 small frame objects and
        # `plan_stream` packs the raw bytes in one vectorised step.  Measured:
        #   per-frame path  iter_chunks 7.9 ms + join 1.2 ms = 9.1 ms
        #   fast path       plan_stream 4.4 ms               = 4.4 ms
        # Byte-identical -- asserted in tests/test_stream_fastpath.py.
        plan = ie.plan_image(src, mode="full", frames=False)
        self.safety.require(WriteClass.IMAGE,
                            f"show_image {plan.pixel_bytes:,} B")

        b = self.batch if batch is None else max(1, int(batch))
        p = self.pause_ms if pause_ms is None else max(0.0, float(pause_ms))
        stream = ie.plan_stream(plan)
        groups = ie.iter_batch_slices(stream, b)

        t0 = time.perf_counter()
        sent = 0
        total = plan.pixel_bytes // ie.BYTES_PER_PIXEL
        n_frames = len(stream) // ie.CHUNK_FRAME
        for group in groups:
            self._transport.write(group)
            sent += len(group) // ie.CHUNK_FRAME
            if p:
                time.sleep(p / 1000.0)
            if on_progress:
                on_progress(sent, n_frames)
        elapsed = time.perf_counter() - t0

        self.stats.frames += sent
        self.stats.bytes_sent += sent * ie.CHUNK_FRAME
        self.stats.record_image(elapsed)
        self.stats.history.append(("show_image", elapsed))
        log.info("frame sent: %d px, %d frames, %.2fs (%.2f MiB/s)",
                 total, sent, elapsed,
                 sent * ie.CHUNK_FRAME / 1048576 / max(elapsed, 1e-9))
        return self.stats

    # -- not yet verified: gated ------------------------------------------

    def set_brightness(self, level: int) -> None:
        """Set brightness via cmd 123.  **NOT hardware-verified.**

        Requires ``allow=(WriteClass.BRIGHTNESS,)``.

        Why it is gated: cmd 123 appears to be a runtime-only one-byte command,
        but the *persistent* brightness lives in the 5-byte configuration command
        (cmd 125, `뽞.cs:7852-7853`).  Whether the device also saves the cmd 123
        value to `/app.cfg` is **not known** -- no recovered call site proves it
        either way.  Treating a possible persistent write as safe would be wrong,
        so it needs an explicit opt-in.
        """
        self._require(WriteClass.BRIGHTNESS, "set_brightness (cmd 123)", (
            "cmd 123 is probably runtime-only, but the persistent brightness lives\n"
            "in cmd 125 (5-byte config -> /app.cfg) and it is NOT proven that the\n"
            "device refrains from saving cmd 123's value. Enable with\n"
            "  SamaScreen(allow=(WriteClass.BRIGHTNESS,))"))
        if not 0 <= int(level) <= 0xFF:
            raise ValueError("brightness must be 0..255")
        self._require_connected()
        self._transport.write(sp.build_brightness(int(level)))
        self.stats.frames += 1
        self.stats.bytes_sent += sp.FRAME_SIZE
        log.info("brightness -> %d (UNVERIFIED command)", level)

    def set_rotation(self, value: int) -> None:
        """Set rotation via cmd 129.  **NOT hardware-verified.**

        Requires ``allow=(WriteClass.ROTATION,)``.  Note that the panel already
        displays our portrait frames at the correct orientation, so rotation is
        very likely unnecessary: the vendor rotates its 1568x720 artwork itself.
        """
        self._require(WriteClass.ROTATION, "set_rotation (cmd 129)", (
            "cmd 129 is untested, and rotation is probably unnecessary: portrait\n"
            "frames already display correctly with no rotation command. Enable with\n"
            "  SamaScreen(allow=(WriteClass.ROTATION,))"))
        if not 0 <= int(value) <= 0xFF:
            raise ValueError("rotation must be 0..255")
        self._require_connected()
        self._transport.write(sp.build_rotate(int(value)))
        self.stats.frames += 1
        self.stats.bytes_sent += sp.FRAME_SIZE
        log.info("rotation -> %d (UNVERIFIED command)", value)

    def update_region(self, image, x: int, y: int) -> None:
        """Partial update.  **Offline encoding exists; hardware is unverified.**

        Requires ``allow=(WriteClass.REGION,)``.  The cmd 204 BGRA delta encoder is
        implemented in ``differ_encoder``, but cmd 202/204/208 have never been sent
        to this panel, so this public operation continues to refuse transmission.
        """
        self._require(WriteClass.REGION, "update_region (cmd 202 + 204 + 208)", (
            "the cmd 204 delta encoder is available for offline use, but the complete\n"
            "202 -> 204 sequence and 208 initialization have not been verified on\n"
            "hardware. Enable with\n"
            "  SamaScreen(allow=(WriteClass.REGION,))  -- only if you accept that"))
        raise UnverifiedOperation(
            "update_region is deliberately not implemented: cmd 204 encoding has "
            "passed offline tests, but the sequence has never been tested on "
            "hardware. See docs/C-THROUGHPUT-HANDOFF-20261026.md.")

    # -- read-only ---------------------------------------------------------

    def ping(self) -> str | None:
        """Send HELLO and return the identity string (one 250-byte frame)."""
        self._require_connected()
        if self.is_mock:
            return self.identity
        self._hello()
        return self.identity

    def safe_area(self):
        """(left, top, right, bottom) of the measured visible region."""
        from encoder import safe_area as _sa
        return _sa(self.panel, SAFE_PAD)

    # -- helpers -----------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected or self._transport is None:
            raise NotConnected("call connect() first (or use a `with` block)")

    def _require(self, cls: WriteClass, what: str, hint: str) -> None:
        try:
            self.safety.require(cls, what)
        except WriteNotAuthorized as exc:
            raise UnverifiedOperation(f"{what} is not authorized.\n{hint}") from exc
        except ForbiddenOperation:
            raise
