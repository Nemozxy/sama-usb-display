# SAMA USB Display — unofficial research driver

**[中文 README](README.md)**

An open-source, unofficial Windows driver and host toolkit for the tested **6.5-inch 720×1568 SAMA/TURZX USB portrait panel** using an Allwinner T113-S3.

This repository is a clean public rewrite of a larger private research workspace. It contains original host tooling, protocol documentation, sanitized measurements, and a Microsoft-sample-derived IddCx driver. It contains no vendor installer, firmware, device images, private certificates, personal media, or raw desktop captures.

## What works

- A Windows UMDF 2.25 / IddCx virtual display with a 720×1568, 30 Hz input mode.
- D3D11 frame capture into a versioned `SAMAIDD1` binary packet format.
- Direct privileged frame reading, or a LocalSystem broker with per-request console-session authorization.
- Python system-dashboard display and direct image output over the panel's CDC-ACM interface.
- Onboard media deployment into fixed storage slots, with host-side preparation, size/MD5 verification, checked sync, atomic replacement, previous-version backup, and T113-local picture display or looping video playback. A custom video has passed a manual cold-boot check.
- Full-frame `cmd 200` output and explicitly gated `cmd 204` differential output.
- Latest-frame-first queues and bounded encode/send overlap.
- Offline mock operation, synthetic WARP capture, protocol tests, and native concurrency tests.

## Supported test device

| Property | Tested value |
|---|---|
| Panel | 6.5-inch portrait, 720×1568 |
| Image interface | USB `1D6B:A065`, CDC-ACM, High-Speed |
| Reported firmware identity | `chs_65inch.dev1_rom1.91` |
| Pixel format | BGRA, 4 bytes per pixel |
| Other USB interface | `1A86:CA65`; not used for image transfer |

COM numbers are assigned by Windows and are not stable identifiers. The host discovers the image interface by USB VID/PID. Other panels, ROM versions, and the earlier encrypted 6.2-inch protocol family are not claimed to be supported.

## Quick start: offline only

Requirements: Windows 10/11 x64, Python 3.12, Visual Studio Build Tools with MSVC x64, a Windows SDK, and PowerShell 7 for the documented commands.

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -r host/requirements-dev.txt
pwsh -NoProfile -File windows/build-host.ps1
./build/host/capture-warp-test.exe ./build/host/warp.frame
./build/host/mailbox-stress-test.exe
$env:PYTHONPATH = 'host'
./.venv/Scripts/python -m pytest -q --basetemp build/pytest-tmp
```

Preview the GUI without opening a serial port:

```powershell
./.venv/Scripts/python host/dashboard_app.py --mock
```

Run a real native frame packet through the complete Python sender using a mock transport:

```powershell
./.venv/Scripts/python host/idd_run.py --packet-file build/host/warp.frame --run
```

Add `--differ` to exercise the differential encoder. Neither command accesses the driver or USB device when used with an offline packet and without `--usb`.

## Driver build and installation

The driver requires the WDK headers and libraries in addition to MSVC and the Windows SDK. See [docs/building.md](docs/building.md). The build scripts never install a driver, create a certificate, change trust settings, enable test signing, or reboot.

The public preview intentionally ships **no private signing key and no one-click system installer**. Installing a self-signed display driver weakens the normal driver-signing boundary and requires an administrator, an explicit signing/trust decision, and usually a reboot. Do not install it on a machine where you cannot recover the display configuration.

## Performance and maturity

The current differential candidate is `--differ-change-threshold 32 --differ-reinit-interval 32`. A sanitized 3,600-frame host-send run completed in 254.691 seconds at **13.87 steady updates/s**, with one connection and no reconnects. This is not a 30/60 fps claim, not an optical panel measurement, and not a 30-minute stability or visual-quality certification. See [docs/performance.md](docs/performance.md).

The full-frame path is much slower because every frame carries about 4.53 MB on the wire. The Windows 30 Hz mode only controls frame production; it does not mean the physical panel displays 30 fps.

Use this as a research preview, not as a production-signed driver. Sleep/resume, all USB recovery cases, non-zero-threshold visual quality, long differential sessions, and broad Windows/hardware compatibility remain incompletely validated.

## Documentation

- [Unified GUI: monitoring, onboard media, and Windows desktop](docs/usage.md)
- [Protocol and hardware findings](docs/protocol.md)
- [Build and verification](docs/building.md)
- [Performance evidence](docs/performance.md)
- [Research findings and limitations](docs/research-findings.md)
- [Sanitized performance evidence](evidence/differ-3600-summary.json) and [onboard-media validation](evidence/onboard-media-validation.json)
- [Contributing, hardware boundaries, and privacy rules](CONTRIBUTING.md)

## License and attribution

Original project code is MIT-licensed. The Microsoft-derived indirect display driver remains under MS-PL; see `windows/idd/driver/LICENSE` and `NOTICE`. SAMA, TURZX, Microsoft, and other names belong to their respective owners. This project is not affiliated with or endorsed by the device vendor.
