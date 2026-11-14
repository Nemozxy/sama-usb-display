from __future__ import annotations

import contextlib
from datetime import datetime
import json
from pathlib import Path
import threading
import uuid

from dashboard_backend import DEFAULT_OUTPUT
from idd_run import DifferFrameSink, PersistentFrameSink, prepare, run_process
from session_lock import controller_lock

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DESKTOP_SECONDS = 86400
DESKTOP_FRAMES = 2592000


class DesktopSession:
    def __init__(self, *, mock=False, output_dir=None, preferred_port=None,
                 broker=False, differ=False, source_fps=15, notify=None):
        self.mock = mock
        self.preferred_port = preferred_port
        self.broker = broker
        self.differ = differ
        self.source_fps = source_fps
        self.notify = notify or (lambda kind, value: None)
        parent = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT / 'desktop'
        self.directory = parent / (datetime.now().strftime('desktop-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
        self.directory.mkdir(parents=True)
        self.cancel = threading.Event()
        self.result = None
        self.summary = None
        self.sink = None
        self.worker = threading.Thread(target=self._run, name='sama-desktop', daemon=False)

    def start(self):
        self.worker.start()

    def shutdown(self, wait_s=10):
        self.cancel.set()
        if self.worker.ident is not None:
            self.worker.join(wait_s)
        return not self.worker.is_alive()

    def _run(self):
        error = None
        try:
            packet = ROOT / 'build' / 'host' / 'warp.frame' if self.mock else None
            command, spec = prepare(packet, seconds=DESKTOP_SECONDS,
                maximum_frames=DESKTOP_FRAMES, replay=self.mock,
                source_fps=self.source_fps, broker=self.broker)
            options = dict(mock=self.mock, output_dir=self.directory / 'transmissions',
                preferred_port=self.preferred_port, notify=self.notify)
            if self.differ:
                self.sink = DifferFrameSink(**options, allow_unverified=True,
                    change_threshold=32, reinit_interval=32)
            else:
                self.sink = PersistentFrameSink(**options)
            lock = contextlib.nullcontext() if self.mock else controller_lock(DATA)
            with lock:
                result = run_process(command, self.sink, maximum_frames=DESKTOP_FRAMES,
                    source_timeout=spec['source_timeout_seconds'], cancel_event=self.cancel,
                    expected_client_sha256=spec['client_sha256'])
        except Exception as exc:
            error = repr(exc)
            spec = dict(desktop_capture_attempted=not self.mock,
                        desktop_capture=False, source='error')
            result = dict(ok=False, errors=[error], frames_received=0,
                          frames_sent=0, source_stopped=False)
        finally:
            if self.sink is not None:
                try:
                    summary = self.sink.close()
                except Exception as exc:
                    close_error = repr(exc)
                    error = (error + '; ' if error else '') + close_error
                    summary = dict(mock=self.mock, device_access=not self.mock,
                        port_released=False, fatal_error=close_error, summary_saved=False)
            else:
                summary = dict(mock=self.mock, device_access=not self.mock,
                    frames_sent=0, port_released=True, fatal_error=error,
                    summary_saved=True)
            if summary.get('fatal_error') or not summary.get('port_released') or summary.get('summary_saved') is False:
                result['ok'] = False
                result.setdefault('errors', []).append('USB会话或关闭失败')
            result.update(spec, ok=result['ok'], device_access=not self.mock,
                desktop_capture_attempted=not self.mock,
                desktop_capture=not self.mock and result.get('frames_received', 0) > 0,
                differ=self.differ, change_threshold=32 if self.differ else 0,
                reinit_interval=32 if self.differ else 0,
                evidence_dir=str(self.directory), usb_summary=summary)
            self.result = result
            self.summary = summary
            try:
                (self.directory / 'result.json').write_text(
                    json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
            except OSError as exc:
                self.summary['summary_saved'] = False
                self.summary['fatal_error'] = repr(exc)
                self.result['ok'] = False
            if self.result['ok'] is False and not self.cancel.is_set():
                details = list(self.result.get('errors') or [error or '桌面会话失败'])
                diagnostic = self.result.get('stderr', '').strip()
                if diagnostic:
                    details.append(diagnostic[:1024])
                self.notify('fatal', '; '.join(details))
