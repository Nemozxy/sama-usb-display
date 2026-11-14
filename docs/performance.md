# Performance evidence

## Measurement boundary

The figures below describe host-side completion of the USB send path on one tested 720×1568 ROM 1.91 panel. They are not LCD optical latency measurements and do not isolate T113 rendering cost. No personal video, raw frames, serial logs, or device-specific captures are included in this repository.

## Full-frame path

Each complete BGRA frame occupies 4,515,840 pixel bytes and 4,534,250 wire bytes including framing. Controlled continuous full-frame runs reached approximately **3.5 updates/s**, consistent with an observed link throughput around 16 MB/s. Increasing the host write batch from 256 to 512 frames did not materially change this result.

## Differential path

The first verified exact-differential continuous video run completed 300 frames at **5.70 updates/s**. Subsequent encoder and transport work added channel masks, small-change filtering, fused scans, efficient `cmd 204` framing, and bounded encode/send overlap.

The current experimental candidate is:

```text
change_threshold = 32
reinit_interval = 32
```

A bounded run then produced:

| Metric | Result |
|---|---:|
| Frames completed | 3,600 / 3,600 |
| Elapsed time | 254.691 s |
| Steady update rate | 13.87 updates/s |
| Initializations | 121 |
| Delta updates | 3,453 |
| Full-frame fallbacks | 26 |
| Total transmitted bytes | 2,031,693,500 |
| Status bytes received | 274,432 |
| Connections / reconnects / offline events | 1 / 0 / 0 |
| Port released | Yes |
| Mean encode time | 17.45 ms |
| Mean send time | 27.96 ms |

The source ledger's SHA-256 was `c0d493d966e40034628a1d04519bd720d9cb61add4bfc31ce39ab75745908ba4`; only aggregate, non-content fields are reproduced here.

## Interpretation and limits

- A 30 Hz Windows display mode is an input cadence, not a physical-panel frame-rate promise.
- Differential throughput depends strongly on workload. Large scene changes can approach or exceed full-frame size and trigger fallback.
- Non-zero thresholds are lossy. Their visual quality has not been formally accepted.
- The 3,600-frame run lasted about 4.25 minutes. It is not a 30-minute or multi-hour differential stability certification.
- A reinit interval of 64 was withdrawn after a later long-run write timeout despite earlier shorter passes.
- No evidence supports advertising native-resolution 60 fps for general full-motion content.

See `evidence/differ-3600-summary.json` for the sanitized machine-readable summary.
