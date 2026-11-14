import contextlib
import gc
import json
import time
import tkinter as tk

import dashboard_app
import onboard_flow
import onboard_media
from dashboard_settings import DEFAULTS


def test_gui_onboard_video_task_cleans_host_workdir(monkeypatch, tmp_path):
    source = tmp_path / 'input.mp4'
    source.write_bytes(b'input')
    onboard_root = tmp_path / 'onboard-data'
    calls = []
    monkeypatch.setattr(dashboard_app, 'load_settings', lambda: dict(DEFAULTS))
    monkeypatch.setattr(dashboard_app, 'save_settings', lambda *args: None)
    monkeypatch.setattr(onboard_flow, 'DATA', onboard_root)

    @contextlib.contextmanager
    def session(port):
        calls.append(('session', port))
        yield

    def prepare(path, folder, fit, rotate, progress=None, target_bytes=None):
        output = folder / 'prepared.mp4'
        output.write_bytes(b'prepared')
        if progress:
            progress(1.0)
        return {'path': output}

    def push(path, progress=None, step=None):
        if step:
            for stage in ('backup', 'upload', 'verify', 'stage', 'commit'):
                step(stage)
        if progress:
            progress(1.0)
        return {'device_path': '/mnt/UDISK/video/sama-own.mp4',
                'verified_on_tmp': True, 'staged_and_verified': True,
                'copied_and_verified': True, 'md5': 'abc'}

    monkeypatch.setattr(onboard_flow, 'session_scope', session)
    monkeypatch.setattr(onboard_flow, 'doctor', lambda: {
        'state': 'BARE_LINES', 'uptime': '50.05', 'config_md5': 'abc123'})
    monkeypatch.setattr(onboard_media, 'prepare', prepare)
    monkeypatch.setattr(onboard_flow, 'push_video', push)
    monkeypatch.setattr(onboard_flow, 'apply_config', lambda path: {'written': True})
    monkeypatch.setattr(onboard_flow, 'reboot_device', lambda: calls.append(('reboot', None)))

    root = tk.Tk()
    root.withdraw()
    app = None
    try:
        app = dashboard_app.DashboardApp(root, mock=True)
        app.mock = False
        app.notebook.select(app.media_tab)
        root.update()
        app.onboard_path.set(str(source))
        app.onboard_reboot.set(True)
        assert app.start_live()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and app.worker is not None:
            root.update()
            time.sleep(0.01)
        assert app.worker is None
        assert app.progress['value'] == 100
        assert (onboard_root / 'last-result.json').is_file(), app.status.get()
        result = json.loads((onboard_root / 'last-result.json').read_text(encoding='utf-8'))
        assert result['ok'] and result['reboot_requested']
        assert calls == [('session', '模拟设备'), ('reboot', None)]
        assert not list(onboard_root.glob('prepare-*'))
        assert '完成' in app.status.get()

        def channel_silent(path, progress=None, step=None):
            raise onboard_flow.ChannelSilent('no reply at all')

        monkeypatch.setattr(onboard_flow, 'push_video', channel_silent)
        assert app.start_live()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and app.worker is not None:
            root.update()
            time.sleep(0.01)
        failure = json.loads((onboard_root / 'last-result.json').read_text(encoding='utf-8'))
        assert not failure['ok'] and failure['stage'] == 'upload'
        assert '拔插' in failure['error'] and app.progress['value'] < 100
        assert '未自动重试' in app.status.get()

        monkeypatch.setattr(onboard_flow, 'rollback_video', lambda: {'rolled_back': True})
        assert app.onboard_recovery('rollback')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and app.worker is not None:
            root.update()
            time.sleep(0.01)
        recovered = json.loads((onboard_root / 'last-result.json').read_text(encoding='utf-8'))
        assert recovered['ok'] and recovered['action'] == 'rollback'
        assert len(list((onboard_root / 'history').glob('*.json'))) == 3
        app.close()
    finally:
        if app is not None and app.live is not None:
            app.live.shutdown()
        if app is not None and app.worker is not None:
            app.worker.join(5)
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()


def test_mock_gui_never_starts_onboard_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_app, 'load_settings', lambda: dict(DEFAULTS))
    monkeypatch.setattr(dashboard_app, 'save_settings', lambda *args: None)
    source = tmp_path / 'input.mp4'
    source.write_bytes(b'input')
    root = tk.Tk()
    root.withdraw()
    try:
        app = dashboard_app.DashboardApp(root, mock=True)
        app.notebook.select(app.media_tab)
        root.update()
        app.onboard_path.set(str(source))
        assert not app.start_onboard()
        assert app.worker is None and '不会上传' in app.status.get()
        app.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()
