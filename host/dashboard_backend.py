"""A1 单帧发送：复用实测的 SamaScreen，逐次重枚举并保留完整流。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

from device import MockTransport, SerialTransport, com_ports_for
from sama_screen import PANEL_PID, PANEL_VID, SamaScreen
from serial_proto import SERIAL_BAUD

SIZE = (720, 1568)
DEFAULT_OUTPUT = (Path(sys.executable).resolve().parent / 'data'
                  if getattr(sys, 'frozen', False) else
                  Path(__file__).resolve().parents[1] / 'data' / 'app')
log = logging.getLogger(__name__)


def discover():
    """只枚举正确的图像接口；辅助 MCU 不在候选列表中。"""
    return com_ports_for(PANEL_VID, PANEL_PID)


class CaptureEvidenceError(OSError):
    """本机证据文件故障，不等同于串口故障。"""


class CaptureTransport:
    """包装已有传输，检查短写；异常立即向上传播，不自动重发。"""

    def __init__(self, transport, directory: Path):
        self.transport = transport
        self.tx = self.rx = None
        try:
            self.tx = (directory / 'tx.bin').open('wb')
            self.rx = (directory / 'rx.bin').open('wb')
        except OSError as exc:
            for stream in (self.tx, self.rx):
                if stream is not None:
                    try:stream.close()
                    except OSError:pass
            raise CaptureEvidenceError('收发记录创建失败：' + repr(exc)) from exc
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_hash = hashlib.sha256()
        self.rx_hash = hashlib.sha256()
        self.closed = False
        self.evidence_error = None
        self.transport_close_error = None

    def _record(self, stream, data):
        # A disk fault must not interrupt an already-started USB frame. Latch
        # it, stop writing evidence, then fail at the caller's frame boundary.
        if self.evidence_error is not None:
            return
        try:
            count = stream.write(data)
            if count != len(data):
                raise OSError(f'记录短写：{count!r}/{len(data)}')
        except OSError as exc:
            self.evidence_error = '收发记录保存失败：' + repr(exc)

    def check_evidence(self):
        if self.evidence_error:
            raise CaptureEvidenceError(self.evidence_error)

    def close_streams(self):
        for stream in (self.tx, self.rx):
            try:stream.close()
            except OSError as exc:
                if self.evidence_error is None:
                    self.evidence_error = '收发记录关闭失败：' + repr(exc)
        self.check_evidence()

    def write(self, data: bytes) -> int:
        count = self.transport.write(data)
        if not isinstance(count, int) or not 0 <= count <= len(data):
            raise OSError(f'串口返回无效写入长度：{count!r}')
        accepted = data[:count]
        self._record(self.tx, accepted)
        self.tx_hash.update(accepted)
        self.tx_bytes += count
        if count != len(data):
            raise OSError(f'串口短写：{count}/{len(data)} 字节；已停止，不重试')
        return count

    def read(self, size=2048, timeout=0.3):
        data = self.transport.read(size, timeout=timeout)
        self._record(self.rx, data)
        self.rx_hash.update(data)
        self.rx_bytes += len(data)
        return data

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.transport.close()
        except Exception as exc:
            self.transport_close_error = repr(exc)
            raise
        finally:
            # Always attempt both stream closes, even when transport/tx fails.
            # Preserve a transport failure as the primary close exception.
            try:self.close_streams()
            except CaptureEvidenceError:
                if self.transport_close_error is None:raise


def send_once(image: Image.Image, *, port: str | None = None, mock: bool = False,
              output_dir: Path = DEFAULT_OUTPUT, progress=None,
              transport_factory=None, discovery=None) -> dict:
    """只发一张 720×1568 图。连接、HELLO 和图像结束后释放端口。

    模拟传输可注入；测试不会调用设备工厂。结果中的“发送完成”不等于
    实体屏目视验收，后者单独保持待确认。
    """
    if image.size != SIZE:
        raise ValueError(f'画布必须为 {SIZE[0]}×{SIZE[1]}，当前为 {image.size}')
    if not mock:
        candidates = (discovery or discover)()
        matches = [p for p in candidates if port is None or p.device == port]
        if len(matches) != 1:
            raise RuntimeError('未找到唯一图像接口。请连接副屏、重新识别并选择 1D6B:A065；'
                               '不要选择辅助 MCU 接口。')
        port = matches[0].device
    else:
        port = '模拟设备'
    directory = Path(output_dir) / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    image = image.convert('RGB')
    image.save(directory / 'sent.png')
    record = {
        'started_at': datetime.now().astimezone().isoformat(),
        'port': port, 'vid_pid': '1D6B:A065', 'mock': mock,
        'canvas': list(SIZE), 'protocol': 'cmd 200 / BGRA / 249+1 / 250 bytes',
        'batch': 256, 'automatic_retries': 0,
        'image_sha256': hashlib.sha256(image.tobytes()).hexdigest(),
        'status': 'started', 'visual_confirmation': 'pending',
        'evidence_dir': str(directory.resolve()),
    }
    capture = None
    screen = None
    transport = None
    started = time.perf_counter()
    try:
        if mock:
            transport = transport_factory(port) if transport_factory else MockTransport()
        else:
            factory = transport_factory or (lambda name: SerialTransport(
                name, baudrate=SERIAL_BAUD, read_timeout=0.15, write_timeout=5.0))
            transport = factory(port)
        capture = CaptureTransport(transport, directory)
        # CaptureTransport 包装模拟对象后，不能再用 isinstance 判断模拟模式。
        screen = SamaScreen(port=port, transport=capture, handshake=not mock,
                            fit_safe_default=False, batch=256, read_timeout=0.15)
        screen.connect()
        capture.check_evidence()
        record['identity'] = screen.identity
        if not mock and not (screen.identity or '').startswith('chs_65inch.'):
            raise RuntimeError('HELLO 未确认 6.5 英寸设备身份；图像未发送。请关闭原厂软件后重新识别。')
        screen.show_image(image, fit_safe=False, on_progress=progress)
        capture.check_evidence()
        record.update(status='mock_completed' if mock else 'host_send_completed',
                      image_seconds=screen.stats.last_image_seconds,
                      image_wire_bytes=screen.stats.bytes_sent)
        log.info('单帧发送完成：%s，%.3f 秒', port, screen.stats.last_image_seconds)
    except Exception as exc:
        record.update(status='failed', error=str(exc))
        log.exception('单帧发送失败，已停止')
        raise
    finally:
        close_error = None
        try:
            if capture:
                capture.close()
            elif transport:
                transport.close()
        except Exception as exc:
            close_error = exc
            prior = record.get('error')
            record.update(status='failed', error=(prior + '; ' if prior else '') + '关闭失败：' + str(exc))
        finally:
            if capture:
                record.update(tx_bytes=capture.tx_bytes, rx_bytes=capture.rx_bytes,
                              tx_sha256=capture.tx_hash.hexdigest(),
                              rx_sha256=capture.rx_hash.hexdigest())
            record['port_released'] = bool(transport is None or
                (capture is not None and capture.closed and capture.transport_close_error is None) or
                (capture is None and close_error is None))
            record['elapsed_seconds'] = round(time.perf_counter() - started, 4)
            (directory / 'result.json').write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        if close_error and sys.exc_info()[0] is None:
            raise close_error
    return record
