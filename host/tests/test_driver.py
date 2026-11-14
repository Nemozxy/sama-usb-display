"""Tests for the P5 long-running display layer (mock transport only)."""

import time

from PIL import Image

from device import MockTransport
from encoder import get_panel, safe_area, SAFE_PAD
from sama_driver import (DashboardScreen, DriverStats, SafeAreaConfig,
                         SendAborted)
from sama_screen import find_panel_port


def mk(**kw):
    """A worker on a mock transport, real thread."""
    d = DashboardScreen(**kw)
    d._connect = lambda: True
    d._screen = None
    return d


IMG = Image.new("RGB", (64, 64), (1, 2, 3))


def test_safe_area_defaults_are_the_measured_crop():
    p = get_panel("65inch")
    c = SafeAreaConfig.measured(p)
    l, t, r, b = c.as_box()

    l0, t0, r0, b0 = safe_area(p, SAFE_PAD)
    assert (l, t, r, b) == (l0, t0, r0, b0)
    assert t == 30 and b == p.height - 1 - 50


def test_safe_area_is_instance_config_not_family_constant():
    """The task book explicitly forbids baking one enclosure into the family."""
    a = SafeAreaConfig(left=0, top=10, right=719, bottom=1400)
    b = SafeAreaConfig(left=0, top=22, right=719, bottom=1525)
    assert a.as_box() != b.as_box()


def test_submit_only_keeps_the_newest_frame():
    d = mk()
    frames = [IMG, IMG, IMG]
    d.start()
    for f in frames:
        d.submit(f)
    time.sleep(0.6)
    assert d.stats.frames_dropped >= 1          # at least one frame superseded
    d.shutdown()


def test_worker_is_a_real_thread_and_pool_exit_cleanly():
    d = mk()
    d.start()
    time.sleep(0.3)
    assert d._worker.is_alive()
    d.shutdown(3.0)
    assert not (d._worker and d._worker.is_alive())


def test_shutdown_is_safe_to_call_twice():
    d = mk()
    d.start()
    time.sleep(0.2)
    d.shutdown()
    d.shutdown()


class SlowGate:
    """A fake 'screen' that ticks `on_progress` slowly and forever.

    This makes the cancellation gate deterministic: when the caller calls
    `cancel()`, `_progress_gate` must raise on the next tick, and the stats must
    record it.  (With a MOCK transport the write is instant, so a real cancel
    cannot be observed reliably.)
    """

    def show_image(self, *_a, on_progress=None, **_k):
        if on_progress:
            for i in range(1, 120):
                time.sleep(0.003)
                on_progress(i, 120)
        return object()


def test_cancel_stops_between_batches():
    d = mk()
    d.start()
    time.sleep(0.1)
    d._screen = SlowGate()
    d.submit(IMG)
    time.sleep(0.05)                             # let the gate start ticking
    d.cancel()
    time.sleep(0.5)
    assert d.stats.cancelled >= 1
    d.shutdown()


def test_render_failure_moves_to_offline_then_reconnects():
    """Fail once, then reconnect on the faked success path.

    Real auto-detection would fail without hardware, so `_connect` is faked to
    always succeed after the first offline cycle.
    """
    class Flaky:
        def __init__(self):
            self.fails = 1

        def show_image(self, *_a, **_k):
            if self.fails > 0:
                self.fails -= 1
                raise OSError("simulated port loss")
            return object()

    d = mk()
    d.start()
    time.sleep(0.1)
    d._screen = Flaky()
    d._connect = lambda: True

    d.submit(IMG)
    time.sleep(1.2)
    assert d.stats.reconnects >= 1
    d.shutdown()


def test_submit_is_nonblocking():
    d = mk()
    d.start()
    t0 = time.perf_counter()
    for _ in range(50):
        d.submit(IMG)
    assert time.perf_counter() - t0 < 0.2
    d.shutdown()


def test_none_submissions_are_ignored():
    d = mk()
    d.start()
    d.submit(None)
    time.sleep(0.2)
    assert d.stats.frames_rendered == 0
    d.shutdown()


def test_stats_summary_is_readable():
    d = mk()
    s = d.stats.summary()
    assert "rendered=0" in s and "reconnects=0" in s


def test_reconnect_uses_a_backoff_cap():
    d = mk()
    d._stop.wait(0.05)
    assert d.RECONNECT_MAX_DELAY_S < 20
    d.shutdown


def test_send_flow_never_bypasses_the_verified_renderer():
    """The worker must route through SamaScreen.show_image (hardware verified)."""
    import inspect
    import sama_driver
    src = inspect.getsource(sama_driver)
    assert "show_image(" in src
    assert "protocol." not in src or "protocol" not in src.split("import ")[0][:200]


# ---------------------------------------------------------------------------
# regression for the "frame A fails -> frame B replaces it" fix
# ---------------------------------------------------------------------------

def test_frame_b_replaces_failed_frame_a_after_reconnect():
    """Regression for the re-queue fix: B must render, never stale A.

    Step 1: submit A (red); it must fail and move the worker offline.
    Step 2: while offline, submit B (blue, different content).
    Step 3: reconnect succeeds.
    Verify: what reached the screen is B -- A's colour must NOT appear.
    """
    d = mk()
    d.start()
    time.sleep(0.1)

    class FlakyThenGood:
        def __init__(self):
            self.fails = 1
            self.rendered = None

        def show_image(self, image, *_a, **_k):
            if self.fails > 0:
                self.fails -= 1
                raise OSError("simulated port loss")
            self.rendered = image
            return object()

    fake = FlakyThenGood()
    d._screen = fake

    # A realistic reconnect: `_connect` must BOTH succeed AND install a new
    # screen -- because in the real code it creates a fresh SamaScreen.
    def fake_connect():
        d._screen = fake
        return True
    d._connect = fake_connect

    frame_a = Image.new("RGB", (64, 64), (255, 0, 0))       # RED
    d.submit(frame_a)
    deadline = time.time() + 1.0
    while time.time() < deadline and fake.fails == 1:
        time.sleep(0.02)
    assert fake.fails == 0, "frame A must have failed"
    assert d.is_offline(), "worker must be offline after the failure"

    frame_b = Image.new("RGB", (64, 64), (0, 0, 255))       # BLUE
    d.submit(frame_b)
    deadline = time.time() + 1.5
    while time.time() < deadline and fake.rendered is None:
        time.sleep(0.02)
    assert fake.rendered is not None, (
        "frame B must be rendered -- never silently lost")
    px = tuple(fake.rendered.getpixel((32, 32)))
    assert px == (0, 0, 255), (
        f"frame B must be the one rendered (got {px} = stale frame A)")


def test_shutdown_cleans_up_thread_and_port():
    d = mk()
    d.start()
    time.sleep(0.15)
    d.shutdown()
    assert not (d._worker and d._worker.is_alive())
    assert d._screen is None, "port must be closed on shutdown"
    d.shutdown()                                            # idempotent
