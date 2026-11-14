"""Transport layer and device discovery.

Contains the `Transport` abstraction, a fully-functional `MockTransport` for
hardware-free testing, a best-effort serial transport, and explicit stubs for
the USB paths that must not be guessed at.

NOTHING IN THIS FILE HAS BEEN VALIDATED AGAINST REAL HARDWARE.
The transport stubs raise rather than pretend to work.
"""

from __future__ import annotations

import abc
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from protocol import build_command


# --------------------------------------------------------------------------
# Known USB identities  [CONFIRMED] evidence/device-table.md
# --------------------------------------------------------------------------
# Recovered from `UsbMonitorL/Version.cs` (63 `ScreenDev` definitions), joined
# against the decrypted string table.  `dev_code` is matched by the vendor as a
# case-insensitive SUBSTRING of the Windows device interface path, not as a USB
# descriptor read (see WinUsbDevice.cs:473-686).

@dataclass(frozen=True)
class DeviceId:
    """A VID/PID pair the vendor software recognises."""

    vid: int
    pid: int
    model_field: str
    display_name: str
    width: int
    height: int
    width_real: int | None = None
    height_real: int | None = None
    transport_hint: str = ""
    notes: str = ""

    @property
    def dev_code(self) -> str:
        """The exact string the vendor matches against the device path."""
        return f"VID_{self.vid:04X}&PID_{self.pid:04X}"

    @property
    def hardware_id_upper(self) -> str:
        return f"VID_{self.vid:04X}&PID_{self.pid:04X}".upper()


# 6.2-inch entries -- the subject of this investigation.
SIX_TWO_INCH: tuple[DeviceId, ...] = (
    DeviceId(0x1CBE, 0xA062, "Dev_62inch_win207lcd", "62inch", 368, 960,
             transport_hint="libusb/WinUSB (Is207Lcd)"),
    DeviceId(0x1CBE, 0xB062, "Dev_62inchV2_win207lcd", "62inch", 448, 1280,
             transport_hint="libusb/WinUSB (Is207Lcd)"),
    DeviceId(0x1CBE, 0xC062, "Dev_62inchV3_win207lcd", "62inch", 448, 1280,
             width_real=480,
             transport_hint="libusb/WinUSB (Is207Lcd)",
             notes="panel is physically 480 wide, only 448 addressable"),
    DeviceId(0x1CBE, 0x0062, "Dev_62inch_winlcd", "62inch", 368, 960,
             transport_hint="IsWinLcd"),
)

# Sibling models that share the 1CBE "win207lcd" protocol family.  Useful for
# matching captures/hardware IDs in the wild.
FAMILY_1CBE: tuple[DeviceId, ...] = SIX_TWO_INCH + (
    DeviceId(0x1CBE, 0x0040, "Dev_4inch_win207lcd", "4inch", 720, 720),
    DeviceId(0x1CBE, 0x0045, "Dev_45inch_win207lcd", "45inch", 1024, 768, height_real=None),
    DeviceId(0x1CBE, 0x0046, "Dev_46inch_win207lcd", "46inch", 320, 960),
    DeviceId(0x1CBE, 0x0050, "Dev_5inch_win207lcd", "5inch", 720, 1280),
    DeviceId(0x1CBE, 0x0055, "Dev_5inch_win207lcd_hid", "5inch", 720, 1280),
    DeviceId(0x1CBE, 0x0068, "Dev_68inch_win207lcd", "68inch", 1080, 2320),
    DeviceId(0x1CBE, 0x0070, "Dev_7inch_win207lcd", "7inch", 800, 1280),
    DeviceId(0x1CBE, 0x0080, "Dev_8inch_win207lcd", "8inch", 800, 1280),
    DeviceId(0x1CBE, 0x0088, "Dev_88inch_win207lcd", "88inch", 480, 1920),
    DeviceId(0x1CBE, 0x00B0, "Dev_11.3inch (approx)", "113inch", 480, 1920,
             width_real=440, notes="model field uncertain"),
    DeviceId(0x1CBE, 0xA065, "Dev_65inch_win207lcd", "65inch", 720, 1472),
    DeviceId(0x1CBE, 0xB065, "Dev_65inch_win207lcd_sama", "65inch", 720, 1568,
             notes="the only model explicitly branded SAMA"),
    DeviceId(0x1CBE, 0x0021, "Dev_21inch_win207lcd", "21inch", 480, 480),
    DeviceId(0x1CBE, 0x0028, "Dev_28inchR_win207lcd", "28inchR", 480, 480),
    DeviceId(0x1CBE, 0x0034, "Dev_34inch_win207lcd", "34inch", 480, 480),
    DeviceId(0x1CBE, 0x0035, "Dev_35inch_win207lcd", "35inch", 640, 480),
    DeviceId(0x1CBE, 0x0092, "Dev_92inch_win207lcd", "92inch", 480, 1920, width_real=464),
    DeviceId(0x1CBE, 0x0109, "Dev_10inch_win207lcd", "10inch", 1200, 1920),
    DeviceId(0x1CBE, 0x0113, "Dev_113inch_win207lcd", "113inch", 480, 1920, width_real=440),
    DeviceId(0x1CBE, 0x0123, "Dev_12inch_win207lcd", "12inch", 720, 1920),
    DeviceId(0x1CBE, 0x0124, "Dev_124inch_win207lcd", "12.4inch", 2560, 666, width_real=2560),
    DeviceId(0x1CBE, 0x0126, "Dev_1268inch_win207lcd", "12.68inch", 1920, 140, width_real=1920),
    DeviceId(0x1CBE, 0x0288, "Dev_288inch_win207lcd", "288inch", 480, 480),
    DeviceId(0x1CBE, 0x0392, "Dev_392inch_win207lcd", "3.92inch", 1080, 1240, width_real=1080),
    DeviceId(0x1CBE, 0x0021, "Dev_21inch_win207lcd_lianli_SLC", "21inch_SLC", 480, 480),
    DeviceId(0x1CBE, 0xB021, "Dev_21inch_win207lcd_lianli_SLC", "21inch_SLC", 480, 480),
    DeviceId(0x1CBE, 0xB034, "Dev_34inch_win207lcd_lianli_SLC", "34inch_SLC", 480, 480),
    DeviceId(0x1CBE, 0xA068, "Dev_68inch_win207lcd_lianli", "68inch", 1080, 2288),
    DeviceId(0x1CBE, 0xA092, "Dev_92inch_win207lcd_LianLi", "92inch", 480, 1920, width_real=464),
)

ALL_KNOWN_IDS: tuple[DeviceId, ...] = FAMILY_1CBE


def lookup(vid: int, pid: int) -> DeviceId | None:
    """Return the vendor's `DeviceId` for a VID/PID pair, if known."""
    for d in ALL_KNOWN_IDS:
        if d.vid == vid and d.pid == pid:
            return d
    return None


# --------------------------------------------------------------------------
# Transport abstraction
# --------------------------------------------------------------------------

class Transport(abc.ABC):
    """Byte-oriented transport to the panel."""

    #: Human-readable description, shown by `demo.py`.
    description = "abstract transport"

    #: True for the in-memory mock (used by the test-suite and `demo.py`).
    is_mock = False

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        """Send raw bytes.  Returns the number of bytes accepted."""

    def read(self, size: int = 512, timeout: float = 1.0) -> bytes:
        """Read up to `size` bytes; may return b'' for transports that cannot read."""
        return b""

    def flush(self) -> None:
        """Discard any buffered data (default: no-op)."""

    def close(self) -> None:
        """Release the transport (default: no-op)."""

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class MockTransport(Transport):
    """In-memory transport that records every write.

    This is the default transport for the prototype: it lets the packet builder,
    the encoder and the whole `SamaScreen` call flow be exercised without
    hardware, and it never pretends a real panel accepted anything.
    """

    description = "mock (in-memory, records writes; nothing is transmitted)"
    is_mock = True

    def __init__(self, scripted_responses: list[bytes] | None = None) -> None:
        self.writes: list[bytes] = []
        self._responses = list(scripted_responses or [])
        self._read_buffer = bytearray()
        self.closed = False
        self.opened_at = time.time()

    def write(self, data: bytes) -> int:
        if self.closed:
            raise RuntimeError("transport is closed")
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size: int = 512, timeout: float = 1.0) -> bytes:
        """Return the next scripted reply.

        A queued **empty** response is treated as a simulated timeout: it returns
        b'' immediately instead of falling through to the next scripted reply.
        That lets a test script "no answer, then a good answer" to exercise the
        client's retry path.
        """
        if self._read_buffer:
            out = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            return out
        if self._responses:
            nxt = self._responses.pop(0)
            if not nxt:
                return b""              # explicit empty == simulated timeout
            self._read_buffer += nxt
            return self.read(size, timeout)
        return b""

    def queue_response(self, data: bytes) -> None:
        """Script a device->host reply for the next read."""
        self._responses.append(bytes(data))

    # -- inspection helpers -------------------------------------------------
    @property
    def total_bytes(self) -> int:
        return sum(len(w) for w in self.writes)

    def dump(self, limit: int = 4) -> str:
        from protocol import hexdump
        out = []
        for i, w in enumerate(self.writes[:limit]):
            out.append(f"--- write #{i + 1} ({len(w)} bytes) ---")
            out.append(hexdump(w, limit=160))
        if len(self.writes) > limit:
            out.append(f"... ({len(self.writes) - limit} more writes)")
        return "\n".join(out)

    def close(self) -> None:
        self.closed = True


class SerialTransport(Transport):
    """CDC-ACM serial transport (the firmware exposes `/dev/ttyGS0`).

    [STRONG INFERENCE] The panel firmware is a Linux userspace program and the
    string `/dev/ttyGS0` (a USB gadget serial port) appears in it
    (`fw/4inch_1.91`), while the MSI installs no driver and bundles
    `RJCP.SerialPortStream`.  That points at in-box `usbser.sys`.

    [UNKNOWN / NEEDS HARDWARE] The baud rate, and whether the panel accepts the
    DES-framed protocol on this port or only a subset of commands, have NOT been
    established.  Pass `baudrate` explicitly; the default is a guess.
    """

    description = "CDC-ACM serial (NEEDS HARDWARE VERIFICATION)"

    def __init__(self, port: str, baudrate: int = 115200,
                 read_timeout: float = 1.0, write_timeout: float = 2.0) -> None:
        try:
            import serial  # noqa: F401
        except ImportError as exc:
            raise ImportError("pyserial is required: pip install pyserial") from exc
        self.port = port
        self.baudrate = baudrate
        self._serial = serial.Serial(
            port=port, baudrate=baudrate, timeout=read_timeout,
            write_timeout=write_timeout,
        )

    def write(self, data: bytes) -> int:
        return self._serial.write(data)

    def read(self, size: int = 512, timeout: float = 1.0) -> bytes:
        self._serial.timeout = timeout
        return self._serial.read(size)

    def flush(self) -> None:
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass


class LibUsbTransport(Transport):
    """libusb / WinUSB transport for the 0x1CBE panel family.

    The vendor drives these panels through `LibUsbDotNet`
    (`목.cs:332` holds a `UsbEndpointWriter`; `목.cs:4617` writes with a 2000 ms
    timeout).  The endpoint addresses are **not** present in the binary -- they
    come from runtime device enumeration -- but independent public reverse
    engineering of the same protocol documents the WinUSB layout as:

        bulk OUT = 0x01,  bulk IN = 0x81

    (source: `slipalison/bezel`, `docs/reverse-engineering/protocol-turing-usb.md`,
    which reconstructs the vendor's device table; corroborated by
    `mathoudebine/turing-smart-screen-python`, `library/lcd/lcd_comm_turing_usb.py`.)

    That is **external corroboration, not verification on your unit**, so the
    defaults below are declared openly and every caller must opt in.  WinUSB
    binding on Windows is normally arranged by the vendor's own `devcon.exe`;
    libusb cannot open the device without it.

    Requires `pyusb`::

        pip install pyusb
    """

    description = "libusb/WinUSB (endpoints from public RE; UNVERIFIED on this unit)"

    def __init__(self, vid: int, pid: int,
                 ep_out: int = 0x01, ep_in: int = 0x81,
                 interface: int = 0, timeout_ms: int = 2000,
                 detach_kernel_driver: bool = False) -> None:
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise ImportError("pyusb is required: pip install pyusb") from exc

        self.vid, self.pid = vid, pid
        self.ep_out, self.ep_in = ep_out, ep_in
        self.timeout_ms = timeout_ms

        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(
                f"no device {vid:04X}:{pid:04X} found. On Windows this usually "
                f"means WinUSB is not bound to the interface -- the vendor's "
                f"devcon.exe normally does that. Do not run devcon blindly; see "
                f"NEXT_STEPS.md."
            )
        self._dev = dev
        self._usb = usb.util
        try:
            if detach_kernel_driver:
                for cfg in dev:
                    for intf in cfg:
                        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                            dev.detach_kernel_driver(intf.bInterfaceNumber)
            dev.set_configuration()
            usb.util.claim_interface(dev, interface)
        except Exception as exc:
            raise RuntimeError(f"failed to claim interface {interface}: {exc}") from exc
        self.interface = interface
        self._out = usb.util.find_descriptor(
            dev.get_active_configuration()[(interface, 0)],
            custom_match=lambda e: usb.util.endpoint_address(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT and e.bEndpointAddress == ep_out,
        )

    def write(self, data: bytes) -> int:
        # usb.core requires a mutable buffer; write in one transfer, matching the
        # vendor's single `Write(buf, 2000, out _)` call (목.cs:4617).
        n = self._dev.write(self.ep_out, bytearray(data), self.timeout_ms)
        return int(n)

    def read(self, size: int = 512, timeout: float = 1.0) -> bytes:
        """Read a reply.  Responses are NOT encrypted; the status byte is 0xC8 at
        offset 8 (or offset 1 for opcode 113).  [Public RE]"""
        try:
            return bytes(self._dev.read(self.ep_in, size,
                                        int(timeout * 1000)))
        except Exception:
            return b""

    def close(self) -> None:
        try:
            self._usb.dispose_resources(self._dev)
        except Exception:
            pass


class HidTransport(Transport):
    """HID transport -- **deliberately not implemented**.

    `빭.cs` uses HidLibrary, but it is only selected for `IsHidDev` models
    (`Monitor.cs:20615-20620`), and `Dev_DesktopMode` (`VID_1A86&PID_AD11`) is
    the only such entry in the device table.  No 6.2-inch model uses HID.
    Moreover most `AbDevice` overrides in `빭.cs` throw `NotImplementedException`,
    so the HID path cannot even display images.
    """

    description = "HID (NOT IMPLEMENTED -- not used by any 6.2-inch model)"

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "No 6.2-inch panel uses the HID path; see the class docstring."
        )


# --------------------------------------------------------------------------
# Discovery (read-only, host-side)
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 30.0) -> str:
    exe = shutil.which(cmd[0])
    if exe is None:
        return ""
    try:
        r = subprocess.run([exe] + cmd[1:], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


@dataclass
class FoundDevice:
    """A PnP device whose hardware ID matches a known panel."""

    instance_id: str
    friendly_name: str
    status: str
    hardware_ids: list[str] = field(default_factory=list)
    matched: DeviceId | None = None


def find_candidate_devices() -> list[FoundDevice]:
    """Enumerate PnP devices and flag any that match a known VID/PID.

    Windows-only, read-only: shells out to `Get-PnpDevice` and never opens,
    binds, or reconfigures anything.
    """
    ids = {d.hardware_id_upper for d in ALL_KNOWN_IDS}
    script = (
        "Get-PnpDevice -PresentOnly | "
        "Select-Object InstanceId,FriendlyName,Status,"
        "@{n='HardwareIDs';e={($_.HardwareID -join ';')}} | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    raw = _run(["pwsh", "-NoProfile", "-Command", script])
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]

    found: list[FoundDevice] = []
    for item in data:
        hw = [h for h in (item.get("HardwareIDs") or "").split(";") if h]
        match = None
        for hid in hw:
            up = hid.upper()
            for d in ALL_KNOWN_IDS:
                if d.hardware_id_upper in up:
                    match = d
                    break
            if match:
                break
        if match or any(any(i in h.upper() for i in ids) for h in hw):
            found.append(FoundDevice(
                instance_id=item.get("InstanceId") or "",
                friendly_name=item.get("FriendlyName") or "",
                status=item.get("Status") or "",
                hardware_ids=hw,
                matched=match,
            ))
    return found


def enumerate_com_ports() -> list[str]:
    """List serial ports (read-only); candidate CDC-ACM endpoints."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports()]


@dataclass
class SerialPortInfo:
    """A serial port that is *confirmed* to belong to a specific USB VID/PID."""

    device: str
    vid: int | None
    pid: int | None
    serial_number: str = ""
    product: str = ""
    manufacturer: str = ""
    hwid: str = ""

    def describe(self) -> str:
        vp = f"{self.vid:04X}:{self.pid:04X}" if self.vid else "?"
        return (f"{self.device}  {vp}  {self.product or self.manufacturer or ''} "
                f"{('sn=' + self.serial_number) if self.serial_number else ''}").strip()


def com_ports_for(vid: int, pid: int | None = None) -> list[SerialPortInfo]:
    """Only serial ports whose USB VID/PID matches -- never a blanket grab.

    Opening an arbitrary COM port could disturb an unrelated device, so the
    auto-transport path must use this instead of `enumerate_com_ports()`.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out: list[SerialPortInfo] = []
    for p in list_ports.comports():
        if getattr(p, "vid", None) != vid:
            continue
        if pid is not None and getattr(p, "pid", None) != pid:
            continue
        out.append(SerialPortInfo(
            device=p.device,
            vid=getattr(p, "vid", None),
            pid=getattr(p, "pid", None),
            serial_number=getattr(p, "serial_number", "") or "",
            product=getattr(p, "product", "") or "",
            manufacturer=getattr(p, "manufacturer", "") or "",
            hwid=getattr(p, "hwid", "") or "",
        ))
    return out


def describe_environment() -> str:
    """A short human-readable report used by `demo.py --scan`."""
    lines = ["=== host device scan (read-only) ==="]
    devs = find_candidate_devices()
    if not devs:
        lines.append("no known VID/PID present "
                     "(or Get-PnpDevice unavailable)")
    for d in devs:
        name = d.matched.display_name if d.matched else "?"
        geo = f"{d.matched.width}x{d.matched.height}" if d.matched else "?"
        lines.append(f"  {d.instance_id}")
        lines.append(f"      name={d.friendly_name!r} status={d.status} "
                     f"match={name} ({geo})")
        for h in d.hardware_ids[:4]:
            lines.append(f"      hwid: {h}")
    ports = enumerate_com_ports()
    lines.append(f"serial ports: {', '.join(ports) if ports else '(none)'}")
    lines.append(f"analysis env: python {os.sys.version.split()[0]}")
    return "\n".join(lines)
