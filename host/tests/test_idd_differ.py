import json
import sys

import pytest
from PIL import Image

import idd_run
from device import MockTransport
from idd_frame_bridge import DesktopFrame, LatestFrameMailbox
from idd_run import DifferFrameSink, ReceiptLog, run_process


class ScriptedMailbox(LatestFrameMailbox):
    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    def take(self):
        return self.frames.pop(0) if self.frames else None


def test_real_differ_requires_explicit_unverified_opt_in(tmp_path):
    with pytest.raises(RuntimeError, match="显式启用"):
        DifferFrameSink(mock=False, output_dir=tmp_path)


def test_real_differ_rejects_other_rom_before_image_write(tmp_path):
    from types import SimpleNamespace
    sink = DifferFrameSink(mock=False, output_dir=tmp_path, allow_unverified=True)
    rendered = []
    sink.driver = SimpleNamespace(
        mock=False, _screen=SimpleNamespace(identity="chs_65inch.dev1_rom1.88"),
        _connect=lambda: True, _render=lambda *args: rendered.append(args))
    with pytest.raises(RuntimeError, match="ROM1.91"):
        sink._send(SimpleNamespace(pixels=b""))
    assert rendered == []


def test_mock_sink_sends_delta_and_closes_one_connection(tmp_path):
    transport = MockTransport()
    sink = DifferFrameSink(mock=True, output_dir=tmp_path, reinit_interval=1,
                           transport_factory=lambda _: transport)
    receipts, transmissions = [], ReceiptLog()
    pixels = Image.new("RGB", (720, 1568), (10, 20, 30)).convert(
        "RGBA").tobytes("raw", "BGRA")
    changed = bytearray(pixels)
    changed[:4] = bytes((1, 2, 3, 255))
    mailbox = ScriptedMailbox([DesktopFrame(1, 1, pixels),
                               DesktopFrame(2, 2, bytes(changed))])
    sink.forward(mailbox, receipts, lambda: None, transmissions)
    assert [item["mode"] for item in receipts] == ["initialize", "delta"]
    assert receipts[1]["command_sequence"] == 0
    assert receipts[1]["wire_bytes"] == 500
    assert transmissions.total == 2
    assert len(transport.writes) == 0
    summary = sink.close()
    assert summary["successful_connections"] == 1
    assert summary["port_released"] and summary["summary_saved"]
    assert summary["differ_counts"] == {"initialize": 1, "delta": 1, "full": 0}
    assert summary["reinit_interval"] == 1
    assert summary["wire_image_bytes"] == 2 * 4534250 + 500
    assert summary["visual_confirmation"] == "pending"
    assert summary["hardware_verified"] is False


class FailDeltaTransport(MockTransport):
    def __init__(self, short=False):
        super().__init__()
        self.short = short
        self.delta_attempts = 0
        self.full_attempts = 0

    def write(self, data):
        if data[:1] == b"\xcc":
            self.delta_attempts += 1
            if self.short:
                return len(data) - 1
            raise OSError("injected delta failure")
        if data[:1] == b"\xc8":
            self.full_attempts += 1
        return super().write(data)


@pytest.mark.parametrize("short", [False, True])
def test_sink_write_failure_has_no_retry_or_full_fallback(tmp_path, short):
    transport = FailDeltaTransport(short=short)
    sink = DifferFrameSink(mock=True, output_dir=tmp_path,
                           transport_factory=lambda _: transport)
    receipts, transmissions = [], ReceiptLog()
    pixels = Image.new("RGB", (720, 1568)).convert("RGBA").tobytes("raw", "BGRA")
    changed = bytes((1, 2, 3, 255)) + pixels[4:]
    mailbox = ScriptedMailbox([DesktopFrame(1, 0, pixels),
                               DesktopFrame(2, 0, changed)])
    with pytest.raises(RuntimeError, match="无重试"):
        sink.forward(mailbox, receipts, lambda: None, transmissions)
    assert transport.delta_attempts == 1 and transport.full_attempts == 1
    assert len(receipts) == 1
    summary = sink.close()
    assert summary["frames_sent"] == 1 and summary["fatal_error"]


def test_real_offline_client_to_mock_differ_end_to_end(tmp_path):
    packet = tmp_path / "source.frame"
    packet.write_bytes((idd_run.ROOT / "build/host/warp.frame").read_bytes())
    command, _ = idd_run.prepare(packet)
    sink = DifferFrameSink(mock=True, output_dir=tmp_path / "transmissions")
    result = run_process(command, sink, maximum_frames=1, source_timeout=5)
    summary = sink.close()
    assert result["ok"] and result["frames_sent"] == 1
    assert result["receipts"][0]["mode"] == "initialize"
    assert result["source_stopped"] and summary["port_released"]
    assert summary["mock"] and not summary["device_access"]


@pytest.mark.parametrize("extra", [
    ["--usb", "--differ"],
    ["--allow-unverified"],
    ["--differ-change-threshold", "1"],
    ["--differ", "--differ-change-threshold", "256"],
    ["--differ-reinit-interval", "32"],
    ["--differ", "--differ-reinit-interval", "301"],
])
def test_cli_rejects_unsafe_switch_combinations_before_running(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["idd_run.py", "--run", "--packet-file",
        str(idd_run.ROOT / "build/host/warp.frame"), *extra])
    monkeypatch.setattr(idd_run, "run_process",
                        lambda *a, **kw: pytest.fail("unsafe CLI reached process execution"))
    with pytest.raises(SystemExit) as stopped:
        idd_run.main()
    assert stopped.value.code == 2


def test_default_cli_remains_hardware_verified_cmd200(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["idd_run.py", "--packet-file",
        str(idd_run.ROOT / "build/host/warp.frame")])
    assert idd_run.main() == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["differ"] is False and spec["hardware_verified"] is True
