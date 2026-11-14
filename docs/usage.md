# Using the unified Windows controller

Install the Python dependencies and build the host tools as described in `building.md`. Then run:

```powershell
./.venv/Scripts/python host/dashboard_app.py
```

To explore every content mode without opening USB or the virtual-display driver:

```powershell
./.venv/Scripts/python host/dashboard_app.py --mock
```

Mock desktop mode replays the synthetic WARP packet from `build/host/warp.frame`; it never reads the real desktop.

The main window has three explicit tabs: `监控面板`, `板载媒体`, and `Windows 桌面`. Device selection and start/stop controls are shared below the tabs. The default 1100×1020 window is covered by a layout regression test so that the bottom controls remain visible without manually enlarging the window.

## 1. Monitoring panel

Choose `实时面板`. The panel shows time, CPU utilization, memory, system-drive usage, and current network receive/transmit rates. `刷新 / 轮播间隔` controls the update interval from 0.5 to 60 seconds. CPU and network rates use successive host samples; the first network sample establishes a baseline.

The ordinary monitoring sender waits after USB disconnection and can reconnect with the latest frame. This is distinct from both an onboard-media deployment task and the strict desktop session.

## 2. Onboard pictures and looping video

Choose the `板载媒体` tab. This path **writes a prepared asset into the panel's storage and lets the T113 decode it locally**. It is not a continuous stream of frames from the PC.

- `视频循环`: prepare an H.264 High / Level 3.2 / yuv420p, 720×1568 MP4 with its `moov` atom at the end, upload it over `cmd111`, verify both size and MD5, then activate the factory video mode. The factory player loops the selected video. Select a 16/32/48/60 MiB target: two-pass encoding derives video bitrate from duration and reserves 5% for container overhead. Long videos may require visibly lower quality; an impossible target is rejected before FFmpeg starts.
- `板载图片`: prepare a 720×1568 PNG, write the fixed image slot, verify it, and activate the factory picture mode. Only the single-PNG path has prior hardware validation; do not assume JPEG playlists or multi-image rotation are supported.
- Choose blurred background, black-bar padding, or crop-to-fill. Landscape input can optionally be rotated 90° clockwise before fitting. Portrait input is not rotated.
- `上传并激活板载媒体` starts one bounded task. The host uses a temporary preparation directory and removes it afterward. The progress bar and status distinguish preparation, byte-based upload, upload verification, staging/sync, atomic commit/readback, and config activation. Only preparation and byte upload show a percentage; the later command phases show their names because their duration is not byte-linear. Post-upload verification and sync can take tens of seconds and no longer appear as a misleading “upload 100%” stall. A failure does not force the bar to 100%, and its record retains any completed upload/config details.
- The panel uses a fixed current-media slot, one fixed staging file, and one fixed previous-video slot. Replacement does not create an unlimited series of files. New media is verified in the staging file and explicitly synced before an atomic same-filesystem rename; the active slot is not truncated in place. The rename and final readback are followed by another checked `sync`. A failed size/digest/sync check does not activate the new asset. Do not disconnect power while a task is running.
- Image display can invoke the on-device `fbviewer` immediately. Video requires a power cycle, or an explicitly selected request to reboot the panel after upload.
- The shell channel has known reliability limits. Video deployment uses short separate backup/verify/stage/commit commands rather than long combined shell commands, and does not add a doctor preflight to the proven path. A silent channel or write timeout stops the task without automatic retry; follow the recovery message rather than repeatedly starting uploads. Upload batches default to the historically verified 1024 wire frames and drain pending status after each batch. Write failures identify the shell operation or cmd111 batch and byte offset in the latest result.
- Every task writes both `data/onboard/last-result.json` and a separate `data/onboard/history/<operation-id>.json` record. Upload explicitly sends the factory `cmd121` stop-video request before cmd111; a failed stop write prevents upload. Playback is intentionally stopped during deployment and is not visually verified by the host. `重启当前配置` only requests a reboot. `恢复上一版并重启` first requires the fixed previous-video backup and then requests a reboot. Neither recovery action is automatic or available in Mock mode.

The selected onboard asset, fit mode, rotation, and activation options are saved locally. Saving a configuration does not start hardware output. Mock mode never uploads media or changes the panel configuration.

## 3. Windows desktop on the panel

Prerequisites:

1. Build `frame-client.exe` with `windows/build-host.ps1`.
2. Install and enable the virtual display driver through your own reviewed signing/installation process.
3. Confirm that Windows shows a working 720×1568 display in Settings.
4. Build `SamaFrameBroker.exe` too if an unprivileged controller will use the broker.

Choose `Windows 桌面` and then:

- **Direct mode**: clear `普通用户帧代理`. The controller must have permission to open the driver's SYSTEM/Administrators-only interface.
- **Broker mode**: select `普通用户帧代理`. The reviewed `SamaFrameBroker` Windows service must already be running. This public preview does not install services automatically.
- **Source rate**: select 4, 10, 15, or 30 source requests/s. This is not the physical panel's achieved frame rate.
- **Differential 32/32**: explicitly enables the current lossy experimental candidate. Leave it off for the simpler full-frame path.

The desktop session is bounded to 24 hours and 2,592,000 source frames, uses one USB connection, and stops on failure rather than automatically reconnecting. Stop completes the current whole-frame write before releasing the port. Switch to another content source only after stopping the desktop session.

The preview window shows a status card rather than continuously mirroring desktop pixels. Actual desktop content is sent to the physical panel.

## Stopping, tray, and evidence

The controller can continue in the system tray. Stop or exit before closing another session. All modes release their resources during shutdown; a failed release is reported rather than treated as success.

Logs and first-frame evidence are stored beneath `data/`. They can contain desktop or media content and local paths. Review them before sharing.
