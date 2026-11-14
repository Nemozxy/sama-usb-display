"""C的主机帧接收/转发内核：BGRA stride校验、单帧邮箱；CLI只跑模拟USB。"""
from dataclasses import dataclass
import argparse
import hashlib
import json
from pathlib import Path
import struct
import threading
import time

from PIL import Image

WIDTH, HEIGHT = 720, 1568
DXGI_FORMAT_B8G8R8A8_UNORM = 87
MAGIC = b'SAMAIDD1'
HEADER = struct.Struct('<8s4I2Q')
MAX_PACKET_BYTES = 8 * 1024 * 1024


def payload_size(header):
    if len(header) != HEADER.size:
        raise ValueError('IDD帧头不完整')
    magic, width, height, pitch, fmt, seq, timestamp = HEADER.unpack(header)
    if magic != MAGIC or (width, height) != (WIDTH, HEIGHT):
        raise ValueError('IDD帧版本或尺寸不符')
    if fmt != DXGI_FORMAT_B8G8R8A8_UNORM:
        raise ValueError('仅支持DXGI BGRA8格式')
    if pitch < width * 4 or pitch > 8192 or pitch % 4:
        raise ValueError('IDD帧row pitch无效')
    size = pitch * height
    if HEADER.size + size > MAX_PACKET_BYTES:
        raise ValueError('帧包大小越界')
    return size


@dataclass(frozen=True)
class DesktopFrame:
    sequence: int
    timestamp_ns: int
    pixels: bytes

    @classmethod
    def from_packet(cls, packet):
        view = memoryview(packet)
        if not HEADER.size <= len(view) <= MAX_PACKET_BYTES:
            raise ValueError('帧包大小越界')
        size = payload_size(view[:HEADER.size])
        _, width, height, pitch, _, seq, timestamp = HEADER.unpack_from(view)
        if len(view) != HEADER.size + size:
            raise ValueError('IDD帧截断或尾随数据')
        # Own a CPU copy before the driver can recycle/unmap its staging buffer.
        payload = view[HEADER.size:]
        pixels = b''.join(payload[y * pitch:y * pitch + width * 4] for y in range(height))
        return cls(seq, timestamp, pixels)

    def image(self):
        return Image.frombytes('RGBA', (WIDTH, HEIGHT), self.pixels, 'raw', 'BGRA').convert('RGB')


def packet_for(image, sequence, timestamp_ns, pitch=WIDTH * 4):
    if image.size != (WIDTH, HEIGHT) or pitch < WIDTH * 4 or pitch > 8192 or pitch % 4:
        raise ValueError('源尺寸或pitch无效')
    raw = image.convert('RGBA').tobytes('raw', 'BGRA')
    payload = b''.join(raw[y * WIDTH * 4:(y + 1) * WIDTH * 4] + bytes(pitch - WIDTH * 4)
                       for y in range(HEIGHT))
    return HEADER.pack(MAGIC, WIDTH, HEIGHT, pitch, DXGI_FORMAT_B8G8R8A8_UNORM,
                       sequence, timestamp_ns) + payload


class LatestFrameMailbox:
    """容量1；慢USB只跳过未发送的旧帧，不截断正在发送的一帧。"""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._sequence = -1
        self.accepted = 0
        self.overwritten = 0

    def put(self, frame):
        with self._condition:
            if self._closed:
                raise RuntimeError('邮箱已关闭')
            if frame.sequence <= self._sequence:
                raise ValueError('帧序号重复或倒退')
            self._sequence = frame.sequence
            self.accepted += 1
            self.overwritten += self._pending is not None
            self._pending = frame
            self._condition.notify()

    def take(self):
        with self._condition:
            self._condition.wait_for(lambda: self._pending is not None or self._closed)
            frame, self._pending = self._pending, None
            return frame

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def abort(self):
        """异常时丢弃尚未发送帧；已在sink中的整帧由调用方完成。"""
        with self._condition:
            self._pending = None
            self._closed = True
            self._condition.notify_all()


def forward(mailbox, sink, receipts=None):
    """sink完成当前整帧才取下一帧；异常向上抛出，不自动重发。"""
    if receipts is None:
        receipts = []
    while (frame := mailbox.take()) is not None:
        started = time.perf_counter_ns()
        sink(frame.image())
        finished = time.perf_counter_ns()
        receipts.append(dict(sequence=frame.sequence, timestamp_ns=frame.timestamp_ns,
                             send_started_ns=started, send_finished_ns=finished,
                             duration_ms=(finished - started) / 1e6,
                             pixel_sha256=hashlib.sha256(frame.pixels).hexdigest()))
    return receipts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame', type=Path, help='使用SAMAIDD1协议的离线帧文件；省略则生成确定测试图')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'data' / 'idd-bridge')
    args = parser.parse_args()
    if args.frame:
        if not HEADER.size <= args.frame.stat().st_size <= MAX_PACKET_BYTES:
            raise ValueError('帧文件大小越界')
        packet = args.frame.read_bytes()
    else:
        from dashboard_render import test_card
        packet = packet_for(test_card(), 1, time.monotonic_ns(), pitch=3072)
    frame = DesktopFrame.from_packet(packet)
    mailbox = LatestFrameMailbox()
    mailbox.put(frame)
    mailbox.close()
    from dashboard_backend import send_once
    transport = []
    receipts = forward(mailbox, lambda image: transport.append(
        send_once(image, mock=True, output_dir=args.output)))
    report = dict(ok=True, real_driver=False, device_access=False,
                  scope='离线IDD帧→去stride→既有cmd200模拟发送；尚无真实Windows虚拟显示器',
                  receipts=receipts, transport=transport)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'transport'}, ensure_ascii=False, indent=2))
