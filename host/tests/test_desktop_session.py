import json
import time

from desktop_session import DESKTOP_FRAMES, DESKTOP_SECONDS, DesktopSession


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_mock_desktop_session_replays_synthetic_frames_and_releases(tmp_path):
    events = []
    session = DesktopSession(mock=True, output_dir=tmp_path, source_fps=30,
        notify=lambda kind, value: events.append((kind, value)))
    session.start()
    try:
        wait_for(lambda: any(kind == 'frame' for kind, value in events))
    finally:
        assert session.shutdown()
    assert session.result['source'] == 'offline_replay'
    assert session.result['frames_sent'] >= 1
    assert session.result['cancelled'] is True
    assert not session.result['device_access'] and not session.result['desktop_capture']
    assert session.summary['mock'] and session.summary['port_released']
    assert session.summary['fatal_error'] is None
    saved = json.loads((session.directory / 'result.json').read_text(encoding='utf-8'))
    assert saved['usb_summary']['port_released'] is True


def test_mock_desktop_differential_session_uses_current_candidate(tmp_path):
    session = DesktopSession(mock=True, output_dir=tmp_path, differ=True, source_fps=30)
    session.start()
    try:
        wait_for(lambda: session.sink is not None and session.sink.rendered >= 1)
    finally:
        assert session.shutdown()
    assert session.summary['differ_counts']['initialize'] == 1
    assert session.summary['change_threshold'] == 32
    assert session.summary['reinit_interval'] == 32
    assert session.summary['port_released']


def test_desktop_session_has_bounded_24_hour_source():
    assert DESKTOP_SECONDS == 86400
    assert DESKTOP_FRAMES == 30 * DESKTOP_SECONDS


def test_gui_desktop_mode_starts_and_stops_mock_session(tmp_path, monkeypatch):
    import gc
    import tkinter as tk
    import dashboard_app as module
    from dashboard_settings import DEFAULTS

    class Session(DesktopSession):
        def __init__(self, **kwargs):
            super().__init__(output_dir=tmp_path, **kwargs)

    monkeypatch.setattr(module, 'DesktopSession', Session)
    monkeypatch.setattr(module, 'load_settings', lambda: dict(DEFAULTS))
    root = tk.Tk()
    root.withdraw()
    obj = None
    session = None
    try:
        obj = module.DashboardApp(root, mock=True)
        obj.persist = lambda: True
        obj.mode.set('Windows 桌面')
        obj.change_content()
        assert obj.start_live()
        session = obj.live
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not (session.sink and session.sink.rendered):
            root.update()
            time.sleep(0.01)
        assert session.sink and session.sink.rendered >= 1
        obj.stop_live()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and obj.live is not None:
            root.update()
            time.sleep(0.01)
        assert obj.live is None and session.summary['port_released']
        assert not session.result['desktop_capture'] and not session.result['device_access']
        obj.close()
    finally:
        if session is not None:
            session.shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()
