# Build and verification

## Prerequisites

- Windows 10/11 x64
- Python 3.12
- Visual Studio 2022 or newer Build Tools, including the MSVC x64 C++ toolset
- A recent Windows 10/11 SDK
- WDK with UMDF 2.25 and IddCx 1.6 headers/libraries for the driver build
- PowerShell 7 for the example commands

No build step requires the physical panel.

## Host tools and tests

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -r host/requirements-dev.txt
pwsh -NoProfile -File windows/build-host.ps1
./build/host/capture-warp-test.exe ./build/host/warp.frame
./build/host/mailbox-stress-test.exe
$env:PYTHONPATH = 'host'
./.venv/Scripts/python -m pytest -q --basetemp build/pytest-tmp
```

`build-host.ps1` uses `vswhere` and the installed SDK. It writes only beneath `build/host` unless an explicit output directory is supplied. It compiles the frame client, broker, synthetic WARP capture test, and mailbox stress test with MSVC warnings treated as errors.

The WARP test generates its own pixels; it does not capture the desktop, enumerate the project driver, or open USB. The Python suite uses mocks and synthetic frames. Native-client integration tests require the `warp.frame` generated above.

## Driver DLL

```powershell
pwsh -NoProfile -File windows/build-driver.ps1 -WdkRoot 'C:\path\to\WDK'
```

If `-WdkRoot` is omitted, the script checks `WDKContentRoot` and then the standard Windows Kits directory. The expected layout contains:

```text
Include/wdf/umdf/2.25/
Include/<sdk-version>/um/iddcx/1.6/
Lib/wdf/umdf/x64/2.25/WdfDriverStubUm.lib
Lib/<sdk-version>/um/x64/iddcx/1.6/iddcxstub.lib
```

Output goes to `build/driver`. The script compiles and inspects the DLL but does not sign, install, or load it.

## Unsigned package

```powershell
pwsh -NoProfile -File windows/package-driver.ps1 -WdkRoot 'C:\path\to\WDK'
```

This copies the DLL, INF, and MS-PL license into `build/driver/package`, runs InfVerif, and invokes Inf2Cat to create an unsigned catalog. On hosts where the standalone Inf2Cat entry point fails before parsing arguments, the explicit `-UsePowerShellCoreHost` option hosts the original Microsoft executable on PowerShell 7 without modifying it or changing system CLR settings.

A generated CAT is not a signature. This repository does not create or trust certificates, enable test signing, invoke `bcdedit`, install the driver, or reboot.

## Real-device use

Real USB output is always explicit:

```powershell
./.venv/Scripts/python host/idd_run.py --run --usb --seconds 30 --max-frames 120
```

Differential output requires both opt-ins:

```powershell
./.venv/Scripts/python host/idd_run.py --run --usb --differ --allow-unverified `
  --differ-change-threshold 32 --differ-reinit-interval 32
```

Close other programs using the panel first. Start with a short bounded run. The strict IDD session stops on failure rather than reconnecting and resending. Evidence is written beneath `data/`; review it before sharing because first-frame images and logs can contain private content.

## Local public-candidate verification

The 2026-11-05 candidate tree completed the MSVC host build, synthetic WARP capture, 160,000-publication mailbox stress test, and **377 Python tests**, followed by a GUI smoke test. Raw and differential CLI sessions each completed one real native-client frame through mock USB. The unified GUI has hidden-Tk desktop and onboard-task tests, tray smoke coverage, media replacement/resource-release tests, and real GIF/FFmpeg loop tests. Onboard tests cover fixed slots, checked sync, atomic staging/commit decisions, bounded cmd111 writes, real media preparation, mock-upload refusal, host temporary-directory cleanup, and granular post-upload progress. The driver DLL was rebuilt as x64 with the `FxDriverEntryUm` export; InfVerif reported `INF is VALID`, and Inf2Cat reported no errors or warnings. The CAT is intentionally `NotSigned`.

Separate bounded hardware checks validated factory-video recovery and one custom-video cold boot through the synced atomic-replacement flow. These are not long-term power-cut, software-reboot, or broad hardware compatibility certifications; see `evidence/onboard-media-validation.json`.

An earlier fresh dependency download was attempted with both the direct connection and the available local proxy, but the network stalled on the first package metadata request. Therefore a newly created virtual environment has **not** completed dependency installation or its separate test run. The verified Python environment was Python 3.12.10 with Pillow 11.3.0, NumPy 2.3.5, and pytest 9.1.1. The earlier clean-environment verification did not access hardware; the later bounded onboard-media validation did.
