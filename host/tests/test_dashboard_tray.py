"""Tray failure isolation without native icons, settings, or device access."""
import queue
import threading
import sys
from types import SimpleNamespace

import pytest

from dashboard_tray import TrayHost
from dashboard_app import DashboardApp


class Value:
    def __init__(self): self.value = ''
    def set(self, value): self.value = value


@pytest.fixture
def app():
    obj = DashboardApp.__new__(DashboardApp)
    obj.mock = True
    obj.instance = obj.media = obj.live = obj.worker = None
    obj.closing = obj.start_pending = obj.media_stop_pending = obj.hide_pending = False
    obj.media_next = None
    obj.events = queue.Queue()
    obj.status = Value()
    obj.device_text = Value()
    obj.progress = {}
    obj.scheduled = []
    obj.window_events = []
    obj.root = SimpleNamespace(after=lambda *args: obj.scheduled.append(args),
        deiconify=lambda: obj.window_events.append('show'),
        lift=lambda: obj.window_events.append('lift'),
        withdraw=lambda: obj.window_events.append('hide'),
        destroy=lambda: obj.window_events.append('destroy'))
    obj.set_busy = lambda value: None
    obj.persist = lambda: True
    obj.stop_button = SimpleNamespace(configure=lambda **kwargs: None, instate=lambda flags: False)
    obj.tray = TrayHost(obj.events)
    return obj


@pytest.mark.parametrize('failure', ['title', 'menu'])
def test_update_failure_is_reported_once_without_escaping(failure):
    events = queue.Queue()
    host = TrayHost(events)
    class Icon:
        @property
        def title(self): return ''
        @title.setter
        def title(self, value):
            if failure == 'title': raise OSError('synthetic title failure')
        def update_menu(self):
            if failure == 'menu': raise OSError('synthetic menu failure')
    host.icon = Icon()
    host.ready.set()
    for _ in range(3): host.update(True, 'running')
    assert host.running
    assert events.qsize() == 1
    kind, detail = events.get_nowait()
    assert kind == 'tray_error' and failure in detail
    assert host.ready.is_set()  # stop must still reach the live backend


def test_stop_failure_does_not_escape_or_claim_a_live_thread_stopped():
    events = queue.Queue()
    host = TrayHost(events)
    calls = []
    def stop():
        calls.append(True)
        raise OSError('synthetic stop failure')
    host.icon = SimpleNamespace(stop=stop)
    host.thread = SimpleNamespace(is_alive=lambda: True)
    host.ready.set()
    for _ in range(3): assert host.stop() is False
    assert calls and host.stopping.is_set()
    assert events.qsize() == 1 and events.get_nowait()[0] == 'tray_error'


def test_failed_update_does_not_disable_stop_and_dead_thread_is_confirmed():
    events = queue.Queue()
    host = TrayHost(events)
    alive = [True]
    def fail(): raise OSError('menu unavailable')
    def stop(): alive[0] = False
    host.icon = SimpleNamespace(title='', update_menu=fail, stop=stop)
    host.thread = SimpleNamespace(is_alive=lambda: alive[0])
    host.ready.set()
    host.update(True, 'running')
    assert host.stop()
    host.post('start')
    assert events.qsize() == 1  # stopping rejects queued tray actions


def test_gui_poll_survives_tray_update_failure_and_processes_next_event(app):
    def fail(): raise OSError('synthetic menu failure')
    app.tray.icon = SimpleNamespace(title='', update_menu=fail)
    app.tray.ready.set()
    app.live = SimpleNamespace()
    app.events.put(('live', ('connected', dict(port='mock', identity=None))))
    app.events.put(('live', ('frame', dict(count=1, seconds=.1))))
    app.poll()
    assert app.progress['value'] == 100 and len(app.scheduled) == 1
    assert app.tray.failed.is_set() and 'show' in app.window_events
    assert '系统托盘不可用' in app.status.value
    app.hide_pending = True
    app.events.put(('tray_ready', None))  # late setup must not hide the restored window
    app.poll()
    assert 'hide' not in app.window_events and len(app.scheduled) == 2
    app.hide_to_tray()
    assert 'hide' not in app.window_events


def test_gui_close_still_stops_display_but_does_not_hide_live_tray_thread(app):
    alive = [True]
    calls = []
    def stop(): raise OSError('synthetic icon stop failure')
    def shutdown(wait_s):
        calls.append(wait_s)
        return True
    app.tray.icon = SimpleNamespace(stop=stop)
    app.tray.thread = SimpleNamespace(is_alive=lambda: alive[0])
    app.tray.ready.set()
    app.live = SimpleNamespace(shutdown=shutdown,
        summary=dict(fatal_error=None, summary_saved=True, port_released=True))
    app.close()
    worker = app.worker
    worker.join(2)
    assert not worker.is_alive() and calls == [.5]
    app.poll()
    assert app.live is None and app.closing
    assert 'show' in app.window_events
    assert 'destroy' not in app.window_events  # failed stop is NOT proof the thread exited
    assert app.scheduled
    alive[0] = False
    app.poll()
    assert app.window_events.count('destroy') == 1


def test_error_diagnostics_are_bounded_and_deduplicated_across_threads():
    events = queue.Queue()
    host = TrayHost(events)
    workers = [threading.Thread(target=host._error, args=('update', 'x'*10000)) for _ in range(8)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert events.qsize() == 1 and len(events.get_nowait()[1]) <= 1024


def test_hidden_tk_real_mock_display_stops_after_tray_failure(tmp_path, monkeypatch):
    import tkinter as tk
    import time
    from PIL import Image
    import dashboard_app as module
    from dashboard_live import LiveScreen, load_settings
    root = tk.Tk()
    root.withdraw()
    monkeypatch.setattr(root, 'deiconify', lambda: None)
    monkeypatch.setattr(root, 'lift', lambda: None)
    errors, drivers = [], []
    root.report_callback_exception = lambda *args: errors.append(repr(args))
    monkeypatch.setattr(module, 'load_settings', lambda: load_settings(tmp_path / 'unused-settings.json'))
    def factory(**kwargs):
        assert kwargs['mock'] is True
        driver = LiveScreen(output_dir=tmp_path / 'sessions', **kwargs)
        drivers.append(driver)
        return driver
    monkeypatch.setattr(module, 'LiveScreen', factory)
    obj = DashboardApp(root, mock=True)
    obj.persist = lambda: True
    obj.image = Image.new('RGB', (720, 1568), '#173159')
    obj.mode.set('测试画面')
    destroyed = []
    original_destroy = root.destroy
    def destroy():
        for callback in root.tk.call('after', 'info'): root.after_cancel(callback)
        original_destroy()
        destroyed.append(True)
    root.destroy = destroy
    def pump(predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            root.update()
            if predicate(): return
            time.sleep(.01)
        raise AssertionError('hidden Tk cleanup deadline exceeded')
    def fail(): raise OSError('synthetic backend update failure')
    host = TrayHost(obj.events)
    host.icon = SimpleNamespace(title='', update_menu=fail, stop=lambda: None)
    host.ready.set()
    obj.tray = host
    try:
        assert obj._start_live_ready()
        driver = obj.live
        pump(lambda: driver.stats.frames_rendered >= 1 and host.failed.is_set())
        obj.stop_live()
        pump(lambda: obj.live is None)
        assert driver.summary['port_released'] and driver.summary['summary_saved']
        assert not driver.summary['device_access'] and driver.summary['fatal_error'] is None
        assert not driver._worker.is_alive() and not errors
        obj.close()
        assert destroyed == [True]
    finally:
        for driver in drivers: assert driver.shutdown()
        if not destroyed: root.destroy()
        import gc
        gc.collect()


@pytest.mark.parametrize('mode', ['setup-error', 'run-error', 'unexpected-exit'])
def test_setup_and_message_loop_failures_use_bounded_event_path(monkeypatch, mode):
    class Icon:
        def __init__(self, *args): pass
        @property
        def visible(self): return False
        @visible.setter
        def visible(self, value):
            if mode == 'setup-error': raise OSError('synthetic setup failure')
        def run(self, setup):
            if mode == 'run-error': raise OSError('synthetic loop failure')
            setup(self)
        def stop(self): pass
    class Menu:
        SEPARATOR = object()
        def __init__(self, *args): pass
    fake = SimpleNamespace(Icon=Icon, Menu=Menu, MenuItem=lambda *args, **kwargs: object())
    monkeypatch.setitem(sys.modules, 'pystray', fake)
    monkeypatch.setitem(sys.modules, 'pystray._util', SimpleNamespace(win32=object()))
    events = queue.Queue()
    host = TrayHost(events)
    host.start()
    host.thread.join(2)
    assert not host.thread.is_alive() and host.failed.is_set() and not host.ready.is_set()
    recorded = []
    while not events.empty(): recorded.append(events.get_nowait())
    assert any(kind == 'tray_error' for kind, _ in recorded)
    assert len(recorded) <= 2 and all(len(str(detail)) <= 1024 for _, detail in recorded)
    assert host.stop()
