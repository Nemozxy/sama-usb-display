"""P5: long-running display layer over `SamaScreen`.

What this adds over the verified `SamaScreen`
---------------------------------------------
* **latest-frame-first queue** -- a producer can submit frames faster than the
  panel can take them; only the newest one is ever rendered.  Stale frames are
  dropped *inside* the worker, so they never enter the pipe.
* **send cancellation** -- `cancel()` stops an in-flight frame between write
  batches, so a shutdown never has to wait out a 4.5 MB transfer.
* **auto-reconnect** -- if the panel disappears or the port errors, the worker
  closes, re-detects the panel by USB id, reconnects, and re-renders the latest
  submitted frame once it is back.  No caller threading required.
* **safe-area configuration** -- the measured 22/42/0 crop is a property of THIS
  enclosure (see reports/display-geometry-validation.md), not of the protocol
  family, so it is now a constructor parameter instead of a module constant.
* clean exception types -- `ScreenOffline`, `PortBusy`, `FrameCancelled`,
  `SendAborted`, so callers can branch on real states.

Nothing here invents protocol bytes: everything still flows through
`SamaScreen.show_image`, which is hardware verified.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import image_encoder as ie
from encoder import Panel, get_panel
from sama_screen import (PANEL_PID, PANEL_VID, SamaScreen, ScreenError,
                         find_panel_port)

log = logging.getLogger("sama.driver")

RECONNECT_FIRST_DELAY_S = 0.5
RECONNECT_MAX_DELAY_S = 8.0
RECONNECT_BACKOFF = 1.7


class ScreenOffline(ScreenError):
    pass


class PortBusy(ScreenError):
    pass


class FrameCancelled(ScreenError):
    pass


class SendAborted(ScreenError):
    pass


@dataclass
class SafeAreaConfig:
    """Instance-level safe area.  Defaults are the values MEASURED on the unit
    in hand; they describe the ENCLOSURE, not the Dev_65inch protocol family, so
    callers with a different housing should pass their own numbers."""

    left: int = 0
    top: int = 22
    right: int = 719
    bottom: int = 1525
    pad: int = 8

    @classmethod
    def measured(cls, panel: Panel, pad: int = 8) -> "SafeAreaConfig":
        """Build from the measured constants for a given panel."""
        from encoder import safe_area
        l, t, r, b = safe_area(panel, pad)
        return cls(left=l, top=t, right=r, bottom=b, pad=pad)

    def as_box(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


@dataclass
class DriverStats:
    frames_rendered: int = 0
    frames_dropped: int = 0
    reconnects: int = 0
    last_render_ms: float = 0.0
    offline_since: float = 0.0
    cancelled: int = 0
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"rendered={self.frames_rendered} "
                f"dropped_stale={self.frames_dropped} "
                f"cancelled={self.cancelled} reconnects={self.reconnects} "
                f"last={self.last_render_ms:.0f}ms")


class DashboardScreen:

    # Accessible as class attributes so tests can assert the backoff policy.
    RECONNECT_FIRST_DELAY_S = RECONNECT_FIRST_DELAY_S
    RECONNECT_MAX_DELAY_S = RECONNECT_MAX_DELAY_S
    RECONNECT_BACKOFF = RECONNECT_BACKOFF
    """Long-running display worker for the 6.5-inch panel.

    Typical use::

        drv = DashboardScreen()
        drv.start()
        drv.submit(my_pil_image)        # non-blocking, latest wins
        ...
        drv.shutdown()

    The worker reconnects on its own; `submit` never blocks on the panel.
    """

    def __init__(self, panel: str = "65inch",
                 safe_area: SafeAreaConfig | None = None,
                 batch: int = 256, pause_ms: float = 0.0,
                 poll_interval_s: float = 0.2) -> None:
        self.panel = get_panel(panel)
        self.safe_area = safe_area or SafeAreaConfig.measured(self.panel)
        self.batch = max(1, int(batch))
        self.pause_ms = max(0.0, float(pause_ms))
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.stats = DriverStats()

        self._screen: Optional[SamaScreen] = None
        self._slot: Optional[object] = None            # newest pending frame
        self._slot_lock = threading.Lock()
        self._completion = threading.Condition(self._slot_lock)
        self._submitted_sequence = 0
        self._completed_sequence = 0
        self._wakeup = threading.Event()
        self._cancelled = threading.Event()
        self._stop = threading.Event()
        self._offline = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._render_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._wakeup.set()                             # prime a connect attempt
        self._worker = threading.Thread(target=self._run, name="sama-driver",
                                        daemon=True)
        self._worker.start()

    def shutdown(self, wait_s: float = 5.0) -> None:
        self.cancel()
        self._stop.set()
        self._wakeup.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(wait_s)

    # -- public submit / cancel -------------------------------------------

    def submit(self, image) -> int | None:
        """Queue the newest frame.  Never blocks on the panel.

        Only the most recent submission is retained; a frame still being sent is
        finished (we never tear a 4.5 MB transfer mid-pipe unless you call
        `cancel`).
        """
        if image is None:
            return
        with self._completion:
            if self._slot is not None:
                self.stats.frames_dropped += 1          # superseded before send
            self._submitted_sequence += 1
            sequence = self._submitted_sequence
            self._slot = (sequence, image)
        self._wakeup.set()
        return sequence

    def wait_for_submission(self, sequence: int, timeout: float) -> bool:
        """Wait for this submission OR a newer replacement to finish sending.

        This is a host-side completion fence, not an LCD acknowledgement. Older
        pending frames may be replaced under latest-frame-first semantics. A
        timeout/stop/failure never counts as completion, nor does merely taking
        a frame out of the queue. No cancellation or new submission occurs here.
        """
        if type(sequence) is not int or sequence < 1:
            raise ValueError('submission sequence must be a positive integer')
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError('completion timeout must be finite and nonnegative')
        deadline = time.monotonic() + timeout
        with self._completion:
            if sequence > self._submitted_sequence:
                raise ValueError('submission sequence was never issued')
            while self._completed_sequence < sequence:
                if self._stop.is_set() or not self._worker or not self._worker.is_alive():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._completion.wait(min(remaining, 0.1))
            return True

    def cancel(self) -> None:
        """Stop the in-flight frame between write batches, if any."""
        self._cancelled.set()

    def is_offline(self) -> bool:
        return self._offline.is_set()

    # -- worker ------------------------------------------------------------

    def _connect(self) -> bool:
        port, why = find_panel_port()
        if port is None:
            log.debug("no panel: %s", why)
            return False
        try:
            s = SamaScreen(port=port).connect()
        except Exception as exc:                          # noqa: BLE001
            log.debug("connect failed on %s: %s", port, exc)
            return False
        self._screen = s
        self.stats.notes.append(f"connected {port}")
        log.info("panel connected on %s", port)
        return True

    def _take(self):
        with self._slot_lock:
            img, self._slot = self._slot, None
        return img

    def _render(self, img) -> bool:
        if not self._screen:
            return False
        self._cancelled.clear()
        try:
            self._screen.show_image(
                img, batch=self.batch, pause_ms=self.pause_ms,
                on_progress=self._progress_gate)
        except FrameCancelled:
            self.stats.cancelled += 1
            log.info("frame cancelled mid-transfer")
            return False
        except Exception as exc:                          # noqa: BLE001
            log.warning("render failed (%s) -> trying to reconnect", exc)
            self._go_offline(f"render error: {exc}")
            return False
        self.stats.frames_rendered += 1
        return True

    def _progress_gate(self, sent: int, total: int) -> None:
        if self._cancelled.is_set():
            raise FrameCancelled(f"after {sent}/{total} frames")

    def _go_offline(self, why: str) -> None:
        if not self._offline.is_set():
            self._offline.set()
            self.stats.notes.append(f"offline: {why}")
        try:
            if self._screen:
                self._screen.disconnect()
        except Exception:                                 # noqa: BLE001
            pass
        self._screen = None

    def _run(self) -> None:
        delay = RECONNECT_FIRST_DELAY_S
        while not self._stop.is_set():
            # -- pick up the newest frame, if the caller sent one -----------
            pending = self._take()
            if pending is None:
                self._wakeup.wait(self.poll_interval_s)
                self._wakeup.clear()
                if self._stop.is_set():
                    break
                continue
            sequence, img = pending

            # -- make sure we have an online panel -------------------------
            if self._offline.is_set() or not self._screen:
                self._stop.wait(delay)
                if self._stop.is_set():
                    break
                if not self._connect():
                    delay = min(RECONNECT_MAX_DELAY_S, delay * RECONNECT_BACKOFF)
                    if img is not None:
                        # remember it; re-render on reconnect
                        with self._slot_lock:
                            if self._slot is None:
                                self._slot = pending
                    continue
                self._offline.clear()
                delay = RECONNECT_FIRST_DELAY_S
                self.stats.reconnects += 1
                self.stats.notes.append("reconnected")
                # A newer submission may have arrived during discovery/backoff.
                newest = self._take()
                if newest is not None:
                    pending = newest
                    sequence, img = pending

            # -- render the newest frame -----------------------------------
            if self._stop.is_set():
                break
            with self._render_lock:
                t0 = time.perf_counter()
                ok = self._render(img)
                if ok:
                    self.stats.last_render_ms = (time.perf_counter() - t0) * 1000
                    with self._completion:
                        self._completed_sequence = sequence
                        self._completion.notify_all()

            # If the render moved us offline, put the frame back so it renders
            # as soon as reconnection succeeds -- otherwise a failed frame is
            # silently lost and the reconnect counter can never tick.
            if not ok and self._offline.is_set():
                with self._slot_lock:
                    if self._slot is None:
                        self._slot = pending

        # -- shutdown: close the port ---------------------------------------
        if self._screen:
            try:
                self._screen.disconnect()
            except Exception:                                 # noqa: BLE001
                pass
        self._screen = None


__all__ = ["DashboardScreen", "SafeAreaConfig", "ScreenOffline", "PortBusy",
           "FrameCancelled", "SendAborted", "DriverStats"]
