# Tested 6.5-inch USB protocol

## Hardware and transport

The tested enclosure exposes two CDC-ACM functions through an internal USB hub:

- `1D6B:A065`: Allwinner T113-S3 Linux gadget, USB High-Speed, 512-byte bulk endpoints. This is the verified image channel.
- `1A86:CA65`: CH552T function, USB Full-Speed. It is associated with wake-up behavior and is not used by this project's image sender.

The image side identifies itself as `chs_65inch.dev1_rom1.91`. A serial port's numeric name is assigned by the host and must not be hard-coded. The configured CDC baud rate does not limit USB bulk throughput.

## Wire frame

Each wire frame is exactly 250 bytes:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 1 | Command |
| 1–2 | 2 | Magic `EF 69` |
| 3–6 | 4 | Big-endian command value |
| 7 | 1 | Extra command-specific value |
| 8–9 | 2 | Reserved zero |
| 10–249 | 240 | Command payload or padding |

After a command frame, bulk data is split into 250-byte wire frames carrying 249 data bytes each. The last frame is zero-padded. No encryption or checksum is used on this path.

## HELLO

`cmd 1`, value `1`, payload bytes `C5 D3`, requests the device identity. The sender requires the exact tested ROM identity before enabling the differential path.

## Full image: cmd 200

1. Send `cmd 200` with value `720 × 1568 × 4 = 4,515,840`.
2. Stream the complete BGRA buffer using 249-byte data payloads.
3. A complete first image occupies 4,534,250 wire bytes including its command frame.

The differential initialization used by the tested ROM sends the same complete `cmd 200` image **twice** before sending deltas.

## Differential update: cmd 204

The encoder compares all four BGRA bytes against the last displayed frame. Changes are flattened in row-major pixel order.

For a run of `N > 1` changed pixels:

| Offset | Size | Meaning |
|---:|---:|---|
| 0–2 | 3 | Big-endian starting pixel index |
| 3–4 | 2 | Big-endian run length |
| 5… | `4N` | New BGRA pixels |

A single changed pixel uses a seven-byte record: the high bit of the first index byte is set and the run-length field is omitted. Runs longer than 65,000 pixels are rejected rather than truncated.

The delta payload ends in `EF 69`. An empty delta is `80 00 00 00 00 00 EF 69`.

The `cmd 204` command contains the delta length and an embedded command sequence. The sequence starts at zero after initialization and increases monotonically. A full-frame fallback is followed by fresh double-`cmd 200` initialization before subsequent deltas.

The implementation can filter small per-channel changes (`change_threshold`) and can periodically force reinitialization (`reinit_interval`). Both default to zero. The current candidate uses 32 for each, but this is lossy and remains an experimental setting.

## Bounded onboard-media route

The tested ROM also exposes a factory file channel and a tightly wrapped shell route used by the onboard-media tab:

- `cmd 121` requests that factory video playback stop before replacement. It is not proof that playback visually stopped.
- `cmd 111` creates a bounded file upload. The host waits for `create_success`, sends fixed-size data frames, and requires the completion marker before verification.
- `cmd 140` executes only short, wrapped commands with explicit reply markers and path allowlists. It is not exposed as an unrestricted shell in the GUI.

Video is uploaded to `/tmp`, verified, copied to a fixed staging file on the persistent media volume, checked and synced, then atomically renamed over the fixed active slot. A fixed previous-video slot supports explicit rollback. Picture deployment follows the same staging and atomic-replacement principle. The 132-byte `/app.cfg` selects the factory picture/video mode and media path; config writes are synced and read back.

These operations are specific to the tested ROM. They do not establish arbitrary codec support, safe power interruption during a write, reliable software reboot, or support for other panel families.

## What is intentionally excluded

This public repository does not implement firmware updates, NAND/MTD access, boot-chain changes, unrestricted factory operations, or wake-up MCU commands. Persistent writes are limited to the documented onboard-media slots and `/app.cfg`. Historical guesses involving `cmd 202`/`cmd 208`, JPEG/PNG commands from other panel families, or six-byte color-quantized records are not the verified native path described above.
