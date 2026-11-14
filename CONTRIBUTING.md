# Contributing

## Scope

This project targets the documented 6.5-inch 720×1568 SAMA/TURZX panel with ROM identity `chs_65inch.dev1_rom1.91`. Do not infer support for other panels or protocol families from a similar enclosure or product name.

Keep the three features distinct:

- Host-decoded monitoring and media sent as display frames.
- Onboard media stored on the panel and decoded by the T113 itself.
- Windows extended-desktop capture through the IddCx driver and frame client.

## Offline development

See `docs/building.md` for prerequisites and native builds. Install Python dependencies and run the default suite without hardware:

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -r host/requirements-dev.txt
$env:PYTHONPATH = 'host'
./.venv/Scripts/python -m pytest -q --basetemp build/pytest-contribution
./.venv/Scripts/python host/dashboard_app.py --smoke-ui
```

Tests must use mocks, temporary directories, or synthetic frames by default. A test must not open a real serial port, installed display driver, or desktop capture merely because it is running on a development machine. Match the existing module structure and dependency set; a new dependency needs a concrete justification rather than an assumption that it is commonly available.

## Hardware changes

Real-device validation is a separate, bounded action, not a side effect of the default test suite. Define the device/profile, command or frame budget, byte/time limit, persistent paths, stop condition, and recovery path before running. Do not hide automatic retries, repeated re-plug requirements, or ignored errors behind a successful summary.

Do not add firmware updates, raw NAND/MTD/UBI access, boot-chain modification, private signing material, or unrestricted device-shell execution. Onboard-media writes must remain within the documented fixed slots and `/app.cfg`, with verification and recovery behavior intact.

Distinguish these claims explicitly:

- Host encoding or send completion.
- Device receipt and digest verification.
- Actual panel presentation and visual quality.
- Cold-boot, long-running, sleep/resume, and repeated power-cycle stability.

One successful run does not prove any of the others.

## Public artifacts and privacy

Use sanitized aggregate evidence such as the files under `evidence/`. Do not commit personal source media, decoded desktop frames, raw TX/RX logs, local usernames or paths, session identifiers, device instance identifiers, credentials, private certificates, vendor installers, firmware, or proprietary application binaries. `data/` and `build/` are local-only output directories, not public evidence sources.

## License and attribution

Original host code and documentation use the MIT license in `LICENSE`. Microsoft-derived driver code retains its MS-PL license and notices under `windows/idd/driver/`; see `NOTICE`. Preserve third-party notices and do not relicense derived driver code as MIT. Separately installed tools such as FFmpeg retain their own licenses and redistribution obligations.
