"""发送边界回归：用注入传输验证，不打开真实端口。"""
import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import dashboard_backend as backend
from dashboard_render import test_card as make_card
from device import MockTransport


class ReplyTransport(MockTransport):
    def __init__(self, reply=b'chs_65inch.dev1_rom1.91'):
        super().__init__([reply.ljust(1024, b'\x00')])


def test_mock_capture_reconstructs_generated_pixels(tmp_path):
    transport = MockTransport()
    image = make_card()
    record = backend.send_once(image, mock=True, output_dir=tmp_path,
                               transport_factory=lambda _: transport)
    wire = b''.join(transport.writes)
    assert len(wire) == 4534250
    assert wire[:10] == bytes.fromhex('C8 EF 69 00 44 E8 00 00 00 00')
    # 独立按实测 249/250 规则取出像素，不调用项目编码器或解析器。
    raw = b''.join(wire[i:i + 249] for i in range(250, len(wire), 250))[:720 * 1568 * 4]
    reconstructed = Image.frombytes('RGBA', image.size, raw, 'raw', 'BGRA').convert('RGB')
    assert reconstructed.tobytes() == image.tobytes()
    assert transport.closed
    directory = tmp_path / record['evidence_dir'].replace('\\', '/').split('/')[-1]
    assert (directory / 'tx.bin').read_bytes() == wire
    assert record['tx_sha256'] == hashlib.sha256(wire).hexdigest()
    assert record['visual_confirmation'] == 'pending'


@pytest.mark.parametrize('ports', [[], [SimpleNamespace(device='A'), SimpleNamespace(device='B')]])
def test_missing_or_ambiguous_device_never_opens_transport(tmp_path, ports):
    def poisoned(_):
        pytest.fail('不应打开任何端口')
    with pytest.raises(RuntimeError, match='唯一图像接口'):
        backend.send_once(make_card(), output_dir=tmp_path, discovery=lambda: ports,
                          transport_factory=poisoned)


def test_unconfirmed_identity_stops_before_image_and_closes(tmp_path):
    transport = ReplyTransport(b'wrong_device')
    with pytest.raises(RuntimeError, match='HELLO'):
        backend.send_once(make_card(), output_dir=tmp_path,
                          discovery=lambda: [SimpleNamespace(device='FAKE')],
                          transport_factory=lambda _: transport)
    assert len(transport.writes) == 1
    assert transport.writes[0][0] == 1
    assert transport.closed
    record = json.loads(next(tmp_path.glob('*/result.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'failed'
    assert record['tx_bytes'] == 250


def test_short_write_stops_without_retry_and_preserves_capture(tmp_path):
    class ShortWriter(ReplyTransport):
        def write(self, data):
            if self.writes:
                self.writes.append(data[:37])
                return 37
            return super().write(data)

    transport = ShortWriter()
    with pytest.raises(OSError, match='短写'):
        backend.send_once(make_card(), output_dir=tmp_path,
                          discovery=lambda: [SimpleNamespace(device='FAKE')],
                          transport_factory=lambda _: transport)
    assert len(transport.writes) == 2  # HELLO + 一次写入，没有后续写入或重发
    assert transport.closed
    record = json.loads(next(tmp_path.glob('*/result.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'failed'
    assert record['tx_bytes'] == 250 + 37


def test_confirmed_identity_sends_one_image_and_closes(tmp_path):
    transport = ReplyTransport()
    record = backend.send_once(make_card(), output_dir=tmp_path,
                               discovery=lambda: [SimpleNamespace(device='FAKE')],
                               transport_factory=lambda _: transport)
    assert record['identity'].startswith('chs_65inch.')
    assert record['tx_bytes'] == 4534250 + 250
    assert record['status'] == 'host_send_completed'
    assert record['visual_confirmation'] == 'pending'
    assert transport.closed


def test_wrong_canvas_rejected_before_discovery(tmp_path):
    def poisoned():
        pytest.fail('错误尺寸不应接触设备')
    with pytest.raises(ValueError, match='720×1568'):
        backend.send_once(Image.new('RGB', (720, 1600)), output_dir=tmp_path, discovery=poisoned)
