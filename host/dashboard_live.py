"""A2 会话适配：身份校验、整帧停止、有限取证与实时指标。"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import psutil

from dashboard_backend import DEFAULT_OUTPUT, CaptureTransport, CaptureEvidenceError, discover
from device import MockTransport, SerialTransport
from sama_driver import DashboardScreen
from sama_screen import SamaScreen
from serial_proto import SERIAL_BAUD
from dashboard_health import SessionHealth
from dashboard_settings import load_settings as _load_settings, save_settings as _save_settings

log = logging.getLogger(__name__)
CONFIG_PATH = DEFAULT_OUTPUT / 'settings.json'


def load_settings(path=CONFIG_PATH):
    return _load_settings(path)


def save_settings(settings, path=CONFIG_PATH):
    return _save_settings(settings, path)


class MetricSampler:
    def __init__(self):
        self.last_network = None

    def sample(self):
        now = time.monotonic()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home().anchor))
        network = psutil.net_io_counters()
        recv_kib = sent_kib = 0.0
        if self.last_network is not None:
            previous_time, previous_recv, previous_sent = self.last_network
            elapsed = now - previous_time
            if elapsed > 0:
                recv_kib = max(0, network.bytes_recv - previous_recv) / elapsed / 1024
                sent_kib = max(0, network.bytes_sent - previous_sent) / elapsed / 1024
        self.last_network = (now, network.bytes_recv, network.bytes_sent)
        return {'cpu': psutil.cpu_percent(interval=None), 'ram': memory.percent,
                'used_gb': memory.used / 1024 ** 3, 'total_gb': memory.total / 1024 ** 3,
                'disk': disk.percent, 'disk_used_gb': disk.used / 1024 ** 3,
                'disk_total_gb': disk.total / 1024 ** 3,
                'net_recv_kib': recv_kib, 'net_sent_kib': sent_kib}


_sampler = MetricSampler()


def sample_metrics():
    return _sampler.sample()


class CheckedTransport:
    """首帧后仅检查短写，不持续保存每秒 4.5 MB 的原始流。"""
    def __init__(self, transport):
        self.transport = transport

    def write(self, data):
        count = self.transport.write(data)
        if count != len(data):
            raise OSError(f'串口短写：{count}/{len(data)}，本帧已停止')
        return count

    def read(self, size=2048, timeout=0.15):
        return self.transport.read(size, timeout=timeout)

    def close(self):
        self.transport.close()


class LiveScreen(DashboardScreen):
    def __init__(self, *, mock=False, preferred_port=None, notify=None,
                 output_dir=DEFAULT_OUTPUT, discovery=None, transport_factory=None, fail_on_offline=False):
        super().__init__()
        self.mock = mock
        self.preferred_port = preferred_port
        self.notify = notify or (lambda kind, value: None)
        self.discovery = discovery or discover
        self.transport_factory = transport_factory
        self.directory = Path(output_dir) / (datetime.now().strftime('live-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
        self.directory.mkdir(parents=True)
        self.capture = None
        self.capture_done = False
        self.connection = 0
        self.health = SessionHealth()
        self.summary = None
        self.port_released = True
        self.port_release_error = None
        self.fail_on_offline = fail_on_offline
        self.evidence_error = None
        self.rx_bytes_render = 0
        self.rx_chunks_render = 0
        self.last_rx_hex = ''

    def _evidence_failed(self, detail):
        # A host evidence failure is not a USB disconnect. Stop, do not retry
        # or reconnect, and preserve the first failure without unbounded logs.
        if self.evidence_error is None:
            self.evidence_error = detail
        self._stop.set()
        self._wakeup.set()
        if self.summary is not None:
            prior = self.summary.get('fatal_error')
            if not prior or detail not in prior:
                self.summary['fatal_error'] = (prior + '; ' if prior else '') + detail
            self.summary.update(host_stability_30min_passed=False, host_stability_1hour_passed=False)

    def _close_capture(self):
        if self.capture:
            try:
                self.capture.close()
            except CaptureEvidenceError as exc:
                self._evidence_failed(str(exc))
                self.capture = None
                return
            except Exception as exc:
                if getattr(self.capture, 'evidence_error', None):
                    self._evidence_failed(self.capture.evidence_error)
                self.port_release_error = repr(exc)
                self.port_released = False
                raise
            self.capture = None

    def _event(self, kind, value, *, emit=True):
        if kind == 'connected':
            self.health.connections += 1
        elif kind == 'offline':
            self.health.offline_events += 1
            if self.fail_on_offline:
                self._stop.set()
                self._wakeup.set()
        self.stats.notes[:] = self.stats.notes[-20:]
        if self.evidence_error is None:
            try:
                with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'time': datetime.now().astimezone().isoformat(),
                                             'kind': kind, 'value': value}, ensure_ascii=False) + chr(10))
            except OSError as exc:
                self._evidence_failed('会话事件日志保存失败：' + repr(exc))
                self.notify('fatal', self.evidence_error)
        if self.evidence_error and kind not in ('fatal', 'stopped'):
            return False
        if kind == 'stopped' and self.summary is not None:
            value = dict(value, error=self.summary['fatal_error'])
        if emit:
            self.notify(kind, value)
        return self.evidence_error is None

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        worker = threading.Thread(target=self._run, name='sama-live', daemon=False)
        worker.start()
        self._worker = worker

    def shutdown(self, wait_s=10):
        # 不调用 cancel：避免把设备留在半帧状态。单次串口写超时 1 秒。
        self._stop.set()
        self._wakeup.set()
        if self._worker:
            self._worker.join(wait_s)
        return not (self._worker and self._worker.is_alive())

    def _connect(self):
        screen = None
        transport = None
        try:
            ports = [] if self.mock else self.discovery()
            names = [p.device for p in ports]
            if self.mock:
                port = '模拟设备'
            elif self.preferred_port in names:
                port = self.preferred_port
            elif len(names) == 1:
                port = names[0]
            else:
                raise RuntimeError('副屏未连接，或存在多个图像接口；请重新识别并选择')
            if self.transport_factory:
                transport = self.transport_factory(port)
            elif self.mock:
                transport = MockTransport()
            else:
                transport = SerialTransport(port, baudrate=SERIAL_BAUD, read_timeout=0.15, write_timeout=1.0)
            self.port_released = False
            self.connection += 1
            # 每次连接只保存首帧收发证据，后续帧只保存指标及耗时。
            directory = self.directory / f'connection-{self.connection:03d}'
            try:directory.mkdir()
            except OSError as exc:
                raise CaptureEvidenceError('连接证据目录创建失败：' + repr(exc)) from exc
            self.capture = CaptureTransport(transport, directory)
            self.capture_done = False
            screen = SamaScreen(port=port, transport=self.capture, handshake=not self.mock,
                                fit_safe_default=False, read_timeout=0.15)
            screen.connect()
            self.capture.check_evidence()
            if not self.mock and not (screen.identity or '').startswith('chs_65inch.'):
                raise RuntimeError('HELLO 身份未确认，已拒绝发送图像')
            self._screen = screen
            if not self._event('connected', {'port': port, 'identity': screen.identity, 'mock': self.mock}):
                self._close_capture()
                self._screen = None
                return False
            return True
        except CaptureEvidenceError as exc:
            self._evidence_failed(str(exc))
            if self.capture:
                self._close_capture()
            elif transport:
                try:transport.close()
                except Exception as close_exc:
                    self.port_release_error = repr(close_exc)
                    self.port_released = False
            self._screen = None
            self._event('fatal', str(exc))
            return False
        except Exception as exc:
            if self.capture:
                self._close_capture()
            elif transport:
                transport.close()
            self._event('offline', str(exc) + '；自动等待重连。若端口被占用，请关闭占用程序。')
            return False

    def _render(self, image, wire=None):
        started = time.perf_counter()
        try:
            # 画面已按实测安全区布局，禁止再次缩放。
            if wire is None:
                self._screen.show_image(image, fit_safe=False, batch=self.batch)
            else:
                import image_encoder as ie
                if len(wire) % ie.CHUNK_FRAME:
                    raise ValueError('预编码发送流包含不完整帧')
                for group_index, group in enumerate(ie.iter_batch_slices(wire, self.batch)):
                    try:
                        self._screen._transport.write(group)
                    except Exception as exc:
                        raise OSError(f'{exc}; cmd={wire[0]} group={group_index} '
                                      f'group_bytes={len(group)}') from exc
                self._screen.stats.frames += len(wire) // ie.CHUNK_FRAME
                self._screen.stats.bytes_sent += len(wire)
                self._screen.stats.record_image(time.perf_counter() - started)
            # 原厂可能异步回传图像状态；有界读取避免长期填满接收缓冲。
            # 只保留计数和末块前64字节，不把异步文本当成目视验收。
            for _ in range(4):
                chunk = self._screen._transport.read(2048, timeout=0.001)
                if not chunk:
                    break
                self.rx_bytes_render += len(chunk)
                self.rx_chunks_render += 1
                self.last_rx_hex = chunk[:64].hex()
            self.stats.frames_rendered += 1
            self.health.frame(time.perf_counter() - started)
            if not self.capture_done:
                self.capture.check_evidence()
                directory = self.directory / f'connection-{self.connection:03d}'
                try:
                    image.save(directory / 'first-frame.png')
                    self.capture.close_streams()
                    (directory / 'capture.json').write_text(json.dumps({
                        'tx_bytes': self.capture.tx_bytes, 'rx_bytes': self.capture.rx_bytes,
                        'tx_sha256': self.capture.tx_hash.hexdigest(),
                        'rx_sha256': self.capture.rx_hash.hexdigest(),
                        'visual_confirmation': 'pending', 'mock': self.mock,
                    }, indent=2), encoding='utf-8')
                except OSError as exc:
                    raise CaptureEvidenceError('首帧证据保存失败：' + repr(exc)) from exc
                # 流文件关闭；串口继续交给严格检查短写的包装器。
                self._screen._transport = CheckedTransport(self.capture.transport)
                self.capture_done = True
            if self.mock:
                # MockTransport 默认积累 writes；长驻模式不保留重复帧。
                self.capture.transport.writes.clear()
            # SamaScreen 的历史列表用于短会话，长驻时只保留最近记录。
            self._screen.stats.history[:] = self._screen.stats.history[-10:]
            return self._event('frame', {'count': self.stats.frames_rendered,
                                        'seconds': self._screen.stats.last_image_seconds})
        except CaptureEvidenceError as exc:
            self._evidence_failed(str(exc))
            self._event('fatal', str(exc))
            return False
        except Exception as exc:
            self._event('offline', f'传输中断：{exc}；等待重新连接')
            self._go_offline(str(exc))
            return False

    def _go_offline(self, why):
        super()._go_offline(why)
        if self.capture:
            self._close_capture()

    def _run(self):
        error = None
        try:
            super()._run()
        except Exception as exc:
            error = repr(exc)
            self._event('fatal', str(exc))
        finally:
            try:
                if self.capture:
                    self._close_capture()
                self.port_released = self.port_release_error is None
                if self.port_release_error:
                    error = (error + '; ' if error else '') + '端口释放失败：' + self.port_release_error
            except Exception as exc:
                error = (error + '; ' if error else '') + '端口释放失败：' + repr(exc)
                self.port_released = False
            self._screen = None
            if self.evidence_error:
                error = (error + '; ' if error else '') + self.evidence_error
            self.summary = self.health.report(mock=self.mock,dropped=self.stats.frames_dropped,
                released=self.port_released,error=error)
            self.summary.update(rx_bytes_render=self.rx_bytes_render,
                                rx_chunks_render=self.rx_chunks_render,
                                last_rx_hex=self.last_rx_hex)
            # Include a stopped-event log failure in the final persisted summary.
            # GUI reads this after shutdown joins the worker, not on this event.
            self._event('stopped', {'frames': self.stats.frames_rendered, 'port_released': self.port_released,
                                    'error': error}, emit=False)
            self.summary['summary_saved'] = True
            try:
                (self.directory/'summary.json').write_text(json.dumps(self.summary,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as exc:
                self.summary['summary_saved'] = False
                self._evidence_failed('会话摘要保存失败：' + repr(exc))
            self.notify('stopped', {'frames': self.stats.frames_rendered, 'port_released': self.port_released,
                                   'error': self.summary['fatal_error'],
                                   'summary_saved': self.summary['summary_saved']})
