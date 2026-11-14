"""跨界面验证：pitch/颜色、底层源生命周期、USB慢时丢旧帧、完整cmd200字节。"""
import struct
import threading

import pytest
from PIL import Image

from idd_frame_bridge import (WIDTH, HEIGHT, HEADER, DesktopFrame, LatestFrameMailbox,
                              packet_for, forward)
from device import MockTransport
from dashboard_backend import send_once


def fixture():
    image = Image.new('RGB', (WIDTH, HEIGHT), (13, 71, 199))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((WIDTH - 1, HEIGHT - 1), (0, 255, 0))
    return image


def test_padded_frame_owns_pixels_and_roundtrips_actual_cmd200(tmp_path):
    source = fixture()
    packet = bytearray(packet_for(source, 99, 123456, pitch=3072))
    frame = DesktopFrame.from_packet(packet)
    packet[:] = bytes(len(packet))
    assert frame.image().tobytes() == source.tobytes()
    transport = MockTransport()
    send_once(frame.image(), mock=True, output_dir=tmp_path, transport_factory=lambda _: transport)
    wire = b''.join(transport.writes)
    assert wire[:10] == bytes.fromhex('C8 EF 69 00 44 E8 00 00 00 00')
    assert len(wire) == 4534250
    pixels = b''.join(wire[offset:offset + 249] for offset in range(250, len(wire), 250))[:WIDTH * HEIGHT * 4]
    actual = Image.frombytes('RGBA', (WIDTH, HEIGHT), pixels, 'raw', 'BGRA').convert('RGB')
    assert actual.tobytes() == source.tobytes()


def test_slow_sink_sends_inflight_then_latest_without_queue_growth():
    mailbox = LatestFrameMailbox()
    pixels = fixture().convert('RGBA').tobytes('raw', 'BGRA')
    began, release = threading.Event(), threading.Event()
    receipts, errors = [], []

    def sink(image):
        assert image.size == (WIDTH, HEIGHT)
        began.set()
        assert release.wait(5)

    def worker():
        try:
            receipts.extend(forward(mailbox, sink))
        except BaseException as exc:
            errors.append(exc)

    mailbox.put(DesktopFrame(1, 0, pixels))
    thread = threading.Thread(target=worker)
    thread.start()
    assert began.wait(5)
    for sequence in range(2, 101):
        mailbox.put(DesktopFrame(sequence, sequence, pixels))
    mailbox.close()
    release.set()
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert [r['sequence'] for r in receipts] == [1, 100]
    assert mailbox.accepted == 100 and mailbox.overwritten == 98
    with pytest.raises(RuntimeError):
        mailbox.put(DesktopFrame(101, 0, pixels))


@pytest.mark.parametrize('change', ['truncated', 'trailing', 'format', 'pitch', 'geometry'])
def test_invalid_packets_never_reach_sink(change):
    packet = bytearray(packet_for(fixture(), 1, 0))
    if change == 'truncated':
        del packet[-1:]
    elif change == 'trailing':
        packet.append(0)
    else:
        offset, value = {'format': (20, 10), 'pitch': (16, 1), 'geometry': (8, 721)}[change]
        struct.pack_into('<I', packet, offset, value)
    with pytest.raises(ValueError):
        DesktopFrame.from_packet(packet)


def test_sequence_rejects_stale_frames_and_send_failure_does_not_retry():
    mailbox = LatestFrameMailbox()
    pixels = fixture().convert('RGBA').tobytes('raw', 'BGRA')
    mailbox.put(DesktopFrame(2, 0, pixels))
    with pytest.raises(ValueError):
        mailbox.put(DesktopFrame(1, 0, pixels))
    mailbox.close()
    calls = []

    def fail(image):
        calls.append(image)
        raise OSError('断线')

    with pytest.raises(OSError, match='断线'):
        forward(mailbox, fail)
    assert len(calls) == 1
