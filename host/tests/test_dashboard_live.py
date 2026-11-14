"""实时会话的失败边界：不访问真实硬件。"""
import json
import threading
import time
from types import SimpleNamespace

from PIL import Image

from dashboard_live import LiveScreen, load_settings, save_settings
from dashboard_render import live_dashboard
from device import MockTransport


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class Reply(MockTransport):
    def __init__(self, identity=b'chs_65inch.dev1_rom1.91'):
        super().__init__([identity.ljust(1024, b'\x00')])


def test_stop_finishes_entire_frame_before_releasing_port(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Gate(Reply):
        def write(self, data):
            if len(data) > 250 and not entered.is_set():
                entered.set()
                assert release.wait(3)
            return super().write(data)
    transport = Gate()
    driver = LiveScreen(output_dir=tmp_path, discovery=lambda: [SimpleNamespace(device='FAKE')],
                        transport_factory=lambda _: transport)
    driver.start()
    driver.submit(Image.new('RGB', (720, 1568), 'blue'))
    try:
        assert entered.wait(3)
        assert not driver.shutdown(wait_s=0.02)
        assert not transport.closed
    finally:
        release.set()
        assert driver.shutdown()
    wire = b''.join(transport.writes)
    assert len(wire) == 4534500
    assert transport.closed
    assert driver.stats.frames_rendered == 1


def test_unconfirmed_identity_never_sends_pixels(tmp_path):
    transport = Reply(b'wrong_device')
    driver = LiveScreen(output_dir=tmp_path, discovery=lambda: [SimpleNamespace(device='FAKE')],
                        transport_factory=lambda _: transport)
    assert not driver._connect()
    assert len(b''.join(transport.writes)) == 250
    assert transport.closed


def test_reconnect_rediscovers_changed_port_and_sends_latest_image(tmp_path):
    failed = threading.Event()
    class Lost(Reply):
        def write(self, data):
            if len(data) > 250:
                failed.set()
                raise OSError('USB lost')
            return super().write(data)
    first, second = Lost(), Reply()
    driver = LiveScreen(output_dir=tmp_path,
        discovery=lambda: [SimpleNamespace(device='NEW' if failed.is_set() else 'OLD')],
        transport_factory=lambda port: second if port == 'NEW' else first)
    driver.start()
    driver.submit(Image.new('RGB', (720, 1568), 'red'))
    try:
        assert failed.wait(3)
        driver.submit(Image.new('RGB', (720, 1568), 'blue'))
        wait_for(lambda: driver.stats.frames_rendered == 1)
    finally:
        assert driver.shutdown()
    assert first.closed and second.closed
    wire = b''.join(second.writes)[250:]
    raw = b''.join(wire[i:i + 249] for i in range(250, len(wire), 250))[:720 * 1568 * 4]
    assert Image.frombytes('RGBA', (720, 1568), raw, 'raw', 'BGRA').getpixel((300, 700)) == (0, 0, 255, 255)


def test_busy_port_is_reported_then_recovers_without_restart(tmp_path):
    busy = threading.Event()
    transport = Reply()
    def factory(port):
        if not busy.is_set():
            busy.set()
            raise PermissionError('port busy')
        return transport
    events = []
    driver = LiveScreen(output_dir=tmp_path, discovery=lambda: [SimpleNamespace(device='FAKE')],
                        transport_factory=factory, notify=lambda k, v: events.append((k, v)))
    driver.start()
    driver.submit(Image.new('RGB', (720, 1568)))
    try:
        wait_for(lambda: driver.stats.frames_rendered == 1)
    finally:
        assert driver.shutdown()
    assert any(k == 'offline' and '占用' in v for k, v in events)
    assert transport.closed


def test_stop_during_discovery_backoff_never_opens_device(tmp_path):
    calls = []
    driver = LiveScreen(output_dir=tmp_path, discovery=lambda: calls.append('opened'))
    driver.submit(Image.new('RGB', (720, 1568)))
    driver.start()
    assert driver.shutdown()
    assert not calls


def test_corrupt_settings_fall_back_and_valid_settings_roundtrip(tmp_path):
    path = tmp_path / 'settings.json'
    for invalid in ('[]', '{bad', '{"interval": "NaN"}', '{"interval": null}'):
        path.write_text(invalid, encoding='utf-8')
        assert load_settings(path)['interval'] == 1
    from dashboard_settings import DEFAULTS
    settings = dict(DEFAULTS, interval=2, theme='琥珀', mode='图片', picture='D:/照片.png')
    save_settings(settings, path)
    assert load_settings(path) == settings


def test_strict_session_stops_on_first_usb_failure_and_records_health(tmp_path):
    calls=[]
    def fail(port):
        calls.append(port)
        raise OSError('lost')
    driver=LiveScreen(output_dir=tmp_path,fail_on_offline=True,
        discovery=lambda:[SimpleNamespace(device='FAKE')],transport_factory=fail)
    driver.start();driver.submit(Image.new('RGB',(720,1568)))
    wait_for(lambda:driver.summary is not None)
    assert driver.shutdown() and calls==['FAKE']
    assert driver.summary['offline_events']==1
    assert not driver.summary['host_stability_30min_passed']
    records=[json.loads(line) for line in (driver.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [r['kind'] for r in records]==['offline','stopped']


def test_close_failure_is_not_reported_as_success(tmp_path):
    class BadClose(Reply):
        def close(self):raise OSError('close failed')
    driver=LiveScreen(output_dir=tmp_path,discovery=lambda:[SimpleNamespace(device='FAKE')],
        transport_factory=lambda p:BadClose())
    driver.start();driver.submit(Image.new('RGB',(720,1568)))
    wait_for(lambda:driver.stats.frames_rendered==1)
    assert driver.shutdown()
    assert not driver.summary['port_released'] and 'close failed' in driver.summary['fatal_error']


def test_render_records_bounded_rx_evidence(tmp_path):
    transport = Reply()
    driver = LiveScreen(output_dir=tmp_path,
                        discovery=lambda: [SimpleNamespace(device='FAKE')],
                        transport_factory=lambda _: transport)
    assert driver._connect()
    transport.queue_response(b'png differ seq\n')
    wire = bytes([204]) + bytes(249)
    assert driver._render(Image.new('RGB', (720, 1568)), wire=wire)
    assert driver.rx_bytes_render == 15
    assert driver.rx_chunks_render == 1
    assert driver.last_rx_hex == b'png differ seq\n'.hex()
    driver._close_capture()


def test_preencoded_write_failure_names_command_and_group(tmp_path):
    events = []

    class Fail(Reply):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def write(self, data):
            self.calls += 1
            if self.calls == 3:
                raise OSError('Write timeout')
            return super().write(data)

    transport = Fail()
    driver = LiveScreen(output_dir=tmp_path,
                        discovery=lambda: [SimpleNamespace(device='FAKE')],
                        transport_factory=lambda _: transport,
                        notify=lambda kind, value: events.append((kind, value)))
    driver.batch = 1
    assert driver._connect()
    wire = (bytes([204]) + bytes(249)) * 2
    assert not driver._render(Image.new('RGB', (720, 1568)), wire=wire)
    offline = [value for kind, value in events if kind == 'offline'][-1]
    assert 'Write timeout' in offline
    assert 'cmd=204 group=1 group_bytes=250' in offline
