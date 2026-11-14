# Research findings and remaining limits

## Established findings

1. The tested panel is a small Linux computer built around an Allwinner T113-S3, not a passive display-only device.
2. The image path is the High-Speed T113 CDC-ACM function. The separate CH552T function is not needed for image transfer.
3. The tested protocol is unencrypted and uses fixed 250-byte wire frames with 249-byte data payloads.
4. Full images are raw 720×1568 BGRA. Native differential initialization uses two complete `cmd 200` images, followed by monotonically sequenced `cmd 204` deltas.
5. The Windows side can create a real virtual display, capture D3D11 frames, transfer them through a controlled user-mode boundary, and send them to the physical panel.
6. Host-side full-frame bandwidth is the obvious limit for raw updates. Differential updates reduce bytes but introduce encoding cost, workload dependence, and device-side reliability questions.
7. Latest-frame-first behavior prevents stale-frame backlog: when the source outruns USB, old queued frames are replaced rather than displayed late.

## Deliberately unpublished material

The original research workspace also contained firmware reads, boot-chain analysis, experimental on-device programs, vendor binaries, raw TX/RX logs, and personal media. Those artifacts are not required to build or use the public host/driver path and are excluded rather than redacted piecemeal.

The public history starts from this clean tree. No claim is made that adding ignore rules to the old repository would have removed data already stored in its history.

## Remaining validation

- Production-quality driver signing and a reversible public installation/uninstallation flow.
- Broad Windows version and hardware coverage.
- Sleep/resume, display reconfiguration, driver reload, and exhaustive USB disconnect behavior.
- Formal visual-quality review for non-zero differential thresholds.
- Long differential sessions of at least 30 minutes and device-side render profiling.
- Better bounded behavior if a low-level operating-system call itself stops responding.
- Onboard-media hardware validation now covers two different 16 MiB-target short videos completing bulk transfer and device-side size/MD5 verification. An earlier in-place `cat > active-file` replacement was followed by changed media bytes on a later black boot; an old unsynced custom-video config also reverted to the factory default. The flow now verifies and syncs a fixed staging file, atomically renames it over the active slot, syncs again, and verifies the final bytes; rollback uses the same pattern, and config writes check `sync`. Factory-video cold boot recovered first, then the replacement short video completed the new atomic flow and visually passed a manual cold boot. This is one successful cold-boot cycle, not a long-term power-cut stability claim. Post-upload software reboot requests remain unreliable. Landscape rotation is an explicit preparation option and was disabled in the control test. Multi-image playlists, arbitrary codecs, and unrestricted shell usage are not claimed. See `evidence/onboard-media-validation.json` for the sanitized machine-readable summary.

## Contributing safely

Keep default tests offline. Use synthetic frames for protocol and pipeline changes. Do not attach personal desktop captures or vendor firmware to issues. Treat any real-device experiment as a separate, bounded action with an explicit frame/byte/time budget, no hidden retry loop, and a defined stop condition.
