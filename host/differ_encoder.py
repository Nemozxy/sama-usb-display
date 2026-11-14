"""cmd 204 sequence-delta and cmd 208 position-info encoders.

The panel's native 65-inch path is cmd 200 (sent twice), then monotonic cmd 204
deltas.  It was visually verified on 2026-10-27: all three probe targets appeared
and the final cmd 200 restored the dark baseline.  `probe_202_208` remains a
separate experimental path; cmd 208 is still not part of the verified native path.
`build_cmd208_frames()` therefore still refuses unless explicitly forced.

------------------------------------------------------------------------------------------
WHAT THIS IMPLEMENTS  (evidence levels in reports/cmd208-protocol-analysis.md)
------------------------------------------------------------------------------------------
`SetPngPosInfo(byte[] img)` (`뽞.cs:6574-6659`) calls

    array = 깬.ꔂ(img, m.screenDev.width, m.screenDev.height, out A_);
    array[A_] = 0xEF; array[A_ + 1] = 0x69; A_ += 2;
    _A9B7(208, A_, null, 0);        // command frame, value = payload length
    _A9B7(array, A_);               // payload, chunked 249 bytes per 250-byte frame

so the payload handed to cmd 208 is  <record stream> || EF 69.

The record stream is produced by `깬.cs:5412`:

    public static byte[] ꔂ(byte[] A_0, int A_1, int A_2, out int A_3,
                           int A_4 = 4, int A_5 = 65000)

i.e. (byte[] src, int width, int height, out int outLen, int bpp = 4,
int maxRun = 65000).

RECORD LAYOUT -- 5 bytes

    [0..2]   pixel index, 24-bit BIG-ENDIAN
    [3..4]   run length, 16-bit BIG-ENDIAN (1 .. maxRun, default max 65000)

There is NO colour information in this record.  That is the single biggest
difference from the dead 6-byte emitter at `깬.cs:4574`, which does carry B/G/R.

STREAM QUIRK -- the final record is over-strided by 5

  * a record closed because the next pixel is "skipped" advances the cursor by 5
    (`깬.cs:5564-5570`, the normal close);
  * a record still open when the pixel loop ends is closed by the "set final" path
    (`깬.cs:5490-5493`, log string `'set final'` at 5494) which advances the cursor
    by **10**.

So `A_3` -- the length the vendor then uses BOTH as the write cursor for the
trailing `EF 69` AND as the cmd 208 `value` field -- is 5 larger than the bytes
actually written whenever the frame ends with an open record.  The result on the
wire is five zero bytes between the last record and the `EF 69` terminator.
This is reproduced exactly (see `final_stride_extra`), because byte-faithfulness is
the only defensible target.  Whether it is intentional or a defect is UNRESOLVED;
see the report's remaining-gaps section.

INPUT FORMAT -- [CONFIRMED] raw 32bppArgb, byte[3] is the ALPHA channel

`A_0` is `currentFrameData`, which `Monitor.cs:5226` builds as
`깬.ꔂ(currentFrame, 4, screenDev.width_real)`.  That overload ends in `쐓`, whose
`ꮙ(int bpp)` locks the bitmap as:

    bpp == 3 -> PixelFormat.Format24bppRgb    (쐓.cs:263)
    bpp == 4 -> PixelFormat.Format32bppArgb   (쐓.cs:284, 289)

and then copies `width * height * bpp` bytes from `Scan0` (`쐓.cs:302-304`).
With bpp = 4 that is a raw BGRA buffer with the alpha channel in byte 3, and the
copy length proves the code assumes NO row padding.

The encoder tests that byte against 15:

    if (A_0[num4 * A_4 + 3] <= 15)  -> skip this pixel      (깬.cs:5471)
    else                            -> "run still open" test (깬.cs:5542)

i.e. a pixel counts as part of the update only when it is essentially opaque.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------------------------------
# Constants  [静态代码直接确认]
# ------------------------------------------------------------------------------------------

RECORD_BYTES = 5
"""Normal record stride: `num3 += 5` at 깬.cs:5570."""

FINAL_STRIDE = 10
"""Stride used by the "set final" path: `num3 += 10` at 깬.cs:5493."""

DEFAULT_MAX_RUN = 65000
"""`A_5 = 65000` (깬.cs:5412).  `num6` is written into a 16-bit field, and 65000 <
65535 leaves margin; the guard forces a run split before the counter overflows.
Nearby log strings read 'cnt overflow:'."""

ALPHA_SKIP_THRESHOLD = 15
"""`A_0[num4 * A_4 + 3] <= 15` means "skip" (깬.cs:5471)."""

DEFAULT_BPP = 4
"""`A_4 = 4`; 쐓.cs:284 maps 4 to Format32bppArgb."""

MAGIC = b"\xEF\x69"
"""Appended by SetPngPosInfo (뽞.cs:6642-6645), giving the terminator on the wire."""

CMD_SET_POS_INFO = 208
"""`_A9B7(208, A_, null, 0)` at 뽞.cs:6646."""


class NotVerified(RuntimeError):
    """Raised instead of transmitting on an unverified path."""


# ------------------------------------------------------------------------------------------
# Result
# ------------------------------------------------------------------------------------------

@dataclass
class PosInfo:
    """Result of the vendor algorithm."""

    records: bytes
    """The record stream as the vendor writes it, truncated to `out_len`."""

    out_len: int
    """`A_3` -- the vendor's reported length, used as the write cursor and as the
    cmd 208 `value` field.  Equals the bytes actually written PLUS `final_gap`."""

    run_count: int
    """Number of records emitted (both normally closed and finally closed)."""

    final_open: bool
    """True when the pixel loop ended with a record still open, i.e. the last
    processed pixel was part of the update.  This is the case that triggers the
    +10 stride."""

    final_gap: int
    """Zero bytes between the last written record byte and `out_len`.
    5 when `final_open`, else 0."""

    ended_with_partial: bool = False
    """True if a forced split landed on the final pixel, where the vendor's
    `A_0[(num4 + 1) * A_4 + 3] = 0` write (깬.cs:5545) would run one element past
    the end of the input array.  The write is skipped here; it provably cannot
    change the output because the pixel loop ends immediately afterwards."""

    def described(self) -> str:
        return (f"records={self.run_count} out_len={self.out_len} "
                f"bytes_written={len(self.records)} final_open={self.final_open} "
                f"gap={self.final_gap}"
                + ("  [forced split on last pixel: OOB write skipped]"
                   if self.ended_with_partial else ""))


# ------------------------------------------------------------------------------------------
# The algorithm
# ------------------------------------------------------------------------------------------

def build_pos_info_records(
    src: bytes | bytearray,
    width: int,
    height: int,
    bpp: int = DEFAULT_BPP,
    max_run: int = DEFAULT_MAX_RUN,
    alpha_threshold: int = ALPHA_SKIP_THRESHOLD,
    mutate_input: bool = False,
) -> PosInfo:
    """Faithful reimplementation of `깬.cs:5412-5652`.

    Parameters
    ----------
    src
        The raw interleaved pixel buffer.  Only `width * height * bpp` bytes are
        read; width and height enter ONLY as their product (see note below).
    width, height
        `A_1`, `A_2`.  The vendor uses them only to compute
        `num9 = A_1 * A_2` (total pixels) and to size the output buffer
        `new byte[A_1 * A_2 * A_4]` (깬.cs:5641-5644).  They do NOT appear in any
        index expression -- pixel `p` is addressed at `src[p * bpp + k]`.  The
        function therefore assumes a flat buffer with no row padding.
    bpp
        `A_4`, bytes per pixel.  A pixel is "in the update" when
        `src[p * bpp + 3] > alpha_threshold`.
    max_run
        `A_5`, the run-length cap.
    mutate_input
        The vendor WRITES into its input array when a forced split fires
        (`A_0[(num4 + 1) * A_4 + 3] = 0`, 깬.cs:5545).  That mutates the caller's
        frame buffer.  Default False works on a copy -- the output is identical,
        only the side effect differs.

    Returns
    -------
    PosInfo
    """
    n_pixels = int(width) * int(height)
    bpp = int(bpp)
    if bpp != 4:
        raise ValueError(
            f"bpp={bpp}: only 4 is used by every recovered call site "
            f"(Monitor.cs:5226 passes 4; 쐓.cs maps 4 to Format32bppArgb)")
    if max_run < 1:
        raise ValueError("max_run must be >= 1")
    need = n_pixels * bpp
    if len(src) < need:
        raise ValueError(f"src too short: need {need} bytes for "
                         f"{width}x{height}x{bpp}, got {len(src)}")

    # The forced-split path WRITES to the buffer (깬.cs:5545), so the working
    # buffer must always be mutable.  With mutate_input=False it is a copy, which
    # is why the output stays identical while the caller's array stays clean.
    if mutate_input:
        if not isinstance(src, bytearray):
            raise TypeError("mutate_input=True requires a bytearray, because the "
                            "vendor writes into the caller's buffer (깬.cs:5545)")
        buf = src
    else:
        buf = bytearray(src[:need])

    # Vendor allocates `new byte[A_1 * A_2 * A_4]` (깬.cs:5644).  Worst case for
    # the record count is an alternating pattern -> n_pixels/2 records -> 2.5*n
    # bytes, which fits; the extra slack below only guards the final +10 stride.
    out = bytearray(n_pixels * bpp + FINAL_STRIDE)

    cursor = 0          # num3
    rec_start = 0       # num5
    run = 0             # num6
    open_rec = False    # flag2
    p = 0               # num4
    run_count = 0
    ended_with_partial = False

    while p < n_pixels:
        alpha = buf[p * bpp + 3]

        # 깬.cs:5471 / 5542 -- "is this pixel part of the update?"
        if alpha <= alpha_threshold:
            in_update = False
        else:
            in_update = run < max_run

        if in_update:
            if not open_rec:
                # 깬.cs:5445-5450 -- open a record, index in 24-bit big-endian
                open_rec = True
                out[cursor] = (p >> 16) & 0xFF
                out[cursor + 1] = (p >> 8) & 0xFF
                out[cursor + 2] = p & 0xFF
                rec_start = cursor
            run += 1                          # 깬.cs:5600
            if run >= max_run:
                # 깬.cs:5545 -- force the split by zeroing the NEXT pixel's flag.
                # The vendor does this unconditionally; if p is the last pixel the
                # write runs one element past the input array.
                nxt = p + 1
                if nxt < n_pixels:
                    buf[nxt * bpp + 3] = 0
                else:
                    ended_with_partial = True
            p += 1
        else:
            if open_rec:
                # 깬.cs:5564-5570 -- normal close, stride 5
                out[rec_start + 3] = (run >> 8) & 0xFF
                out[rec_start + 4] = run & 0xFF
                open_rec = False
                run = 0
                cursor += RECORD_BYTES
                run_count += 1
            p += 1

    final_open = open_rec
    if final_open:
        # 깬.cs:5490-5493 -- the "set final" path, stride 10
        out[rec_start + 3] = (run >> 8) & 0xFF
        out[rec_start + 4] = run & 0xFF
        cursor += FINAL_STRIDE
        run_count += 1

    written = cursor - (FINAL_STRIDE - RECORD_BYTES if final_open else 0)
    return PosInfo(records=bytes(out[:written]),
                   out_len=cursor,
                   run_count=run_count,
                   final_open=final_open,
                   final_gap=cursor - written,
                   ended_with_partial=ended_with_partial)


def build_pos_info_payload(src: bytes | bytearray, width: int, height: int,
                           **kw) -> tuple[bytes, int, PosInfo]:
    """Records plus the `EF 69` terminator, exactly as SetPngPosInfo assembles it.

    Returns (payload, declared_length, info).  The vendor writes the terminator
    at `out_len` in a zero-initialized array, so the final gap precedes `EF 69`
    and is included in both the payload and the declared length.
    """
    info = build_pos_info_records(src, width, height, **kw)
    payload = info.records + bytes(info.final_gap) + MAGIC
    declared = info.out_len + len(MAGIC)
    return payload, declared, info


def cmd208_value_matches_payload(src: bytes | bytearray, width: int,
                                 height: int, **kw) -> bool:
    """Check that the assembled wire payload length matches the declared value."""
    payload, declared, _ = build_pos_info_payload(src, width, height, **kw)
    return declared == len(payload)


# ------------------------------------------------------------------------------------------
# Framing -- present but refuses to be used
# ------------------------------------------------------------------------------------------

def build_cmd208_frames(src: bytes | bytearray, width: int, height: int,
                        allow_unverified: bool = False, **kw) -> list[bytes]:
    """Build the cmd 208 command frame plus its 250-byte data frames.

    Refuses by default.  cmd 208 has NEVER been transmitted to the panel in hand,
    and the 202/208 pairing is inferred rather than verified.  The trailing gap is
    explicitly included before `EF 69` so the declared and actual lengths agree.
    Pass `allow_unverified=True` only for offline inspection of the bytes.
    """
    if not allow_unverified:
        raise NotVerified(
            "cmd 208 is not verified: it has never been sent to the panel, and the "
            "202 -> data -> 208 ordering is inferred. The final-record gap is "
            "included before EF 69. See reports/cmd208-protocol-analysis.md. "
            "Pass allow_unverified=True only to inspect the bytes offline."
        )

    import image_encoder as ie
    import serial_proto as sp

    payload, declared, _ = build_pos_info_payload(src, width, height, **kw)
    cmd = sp.build_frame(CMD_SET_POS_INFO, declared, b"", 0)
    return [cmd] + ie.iter_chunks(payload)


def describe_record(rec: bytes) -> dict:
    """Decode one 5-byte record.  Raises on a wrong length."""
    if len(rec) != RECORD_BYTES:
        raise ValueError(f"a record is {RECORD_BYTES} bytes, got {len(rec)}")
    return {
        "index": (rec[0] << 16) | (rec[1] << 8) | rec[2],
        "run": (rec[3] << 8) | rec[4],
    }


def parse_pos_info(payload: bytes, expect_magic: bool = True) -> list[dict]:
    """Split an assembled cmd 208 payload back into records.

    Stops at the `EF 69` terminator when present.  This is a debugging helper for
    captured traffic, not a validated device-side parser.
    """
    body = payload
    if expect_magic:
        i = payload.find(MAGIC)
        if i < 0:
            raise ValueError("no EF 69 terminator found in the payload")
        body = payload[:i]
    if len(body) % RECORD_BYTES:
        raise ValueError(f"payload body {len(body)} is not a multiple of "
                         f"{RECORD_BYTES}; trailing gap present?")
    return [describe_record(body[i:i + RECORD_BYTES])
            for i in range(0, len(body), RECORD_BYTES)]


# ------------------------------------------------------------------------------------------
# cmd 204 -- ROM >= 1.89 sequence deltas (깬.cs:6134-6432, 뽞.cs:6769-6960)
# ------------------------------------------------------------------------------------------

CMD_UPDATE_BITMAP = 204
SEQUENCE_RECORD_HEADER = 5
SEQUENCE_EMPTY = b"\x80\x00\x00\x00\x00\x00"
SEQUENCE_MAX_RUN = 65000


@dataclass(frozen=True)
class SequenceDelta:
    records: bytes
    changed_pixels: int
    run_count: int

    @property
    def payload(self) -> bytes:
        body = self.records if self.changed_pixels else SEQUENCE_EMPTY
        return body + MAGIC


def _build_sequence_delta_scalar(previous, current, max_run: int) -> SequenceDelta:
    out = bytearray()
    run_start = -1
    run_length = 0
    changed_pixels = 0
    run_count = 0

    def close_run() -> None:
        nonlocal run_start, run_length, run_count
        if run_start < 0:
            return
        if run_length == 1:
            out[run_start] |= 0x80
            out[run_start + 3:run_start + 7] = out[run_start + 5:run_start + 9]
            del out[run_start + 7:run_start + 9]
        else:
            out[run_start + 3] = (run_length >> 8) & 0xFF
            out[run_start + 4] = run_length & 0xFF
        run_start = -1
        run_length = 0
        run_count += 1

    for p in range(len(current) // DEFAULT_BPP):
        offset = p * DEFAULT_BPP
        if previous[offset:offset + DEFAULT_BPP] == current[offset:offset + DEFAULT_BPP]:
            close_run()
            continue
        if run_start < 0:
            run_start = len(out)
            out += bytes(((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF, 0, 0))
        out += current[offset:offset + DEFAULT_BPP]
        run_length += 1
        changed_pixels += 1
        if run_length > max_run:
            raise ValueError(
                f"changed run exceeds {max_run} pixels; the vendor rejects this delta")
    close_run()
    return SequenceDelta(bytes(out), changed_pixels, run_count)


def _build_sequence_delta_from_changed(new, changed, max_run: int, np) -> SequenceDelta:
    padded = np.empty(len(changed) + 2, dtype=bool)
    padded[0] = padded[-1] = False
    padded[1:-1] = changed
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts = edges[::2]
    ends = edges[1::2]
    lengths = ends - starts
    if len(lengths) and int(lengths.max()) > max_run:
        raise ValueError(
            f"changed run exceeds {max_run} pixels; the vendor rejects this delta")

    changed_pixels = int(lengths.sum())
    if not len(starts):
        return SequenceDelta(b"", 0, 0)

    singles = lengths == 1
    sizes = np.where(singles, 7, SEQUENCE_RECORD_HEADER + lengths * DEFAULT_BPP)
    offsets = np.concatenate((np.zeros(1, dtype=np.int64),
                              np.cumsum(sizes[:-1], dtype=np.int64)))
    total = int(sizes.sum())
    out = np.empty(total, dtype=np.uint8)
    out[offsets] = (starts >> 16).astype(np.uint8) | (singles.astype(np.uint8) << 7)
    out[offsets + 1] = (starts >> 8).astype(np.uint8)
    out[offsets + 2] = starts.astype(np.uint8)
    multi = ~singles
    out[offsets[multi] + 3] = (lengths[multi] >> 8).astype(np.uint8)
    out[offsets[multi] + 4] = lengths[multi].astype(np.uint8)

    payload_slots = np.ones(total, dtype=bool)
    payload_slots[(offsets[:, None] + np.arange(3)).reshape(-1)] = False
    if multi.any():
        multi_headers = offsets[multi, None] + np.arange(SEQUENCE_RECORD_HEADER)
        payload_slots[multi_headers.reshape(-1)] = False
    out[payload_slots] = new[changed].reshape(-1)
    return SequenceDelta(out.tobytes(), changed_pixels, len(starts))


def _build_sequence_delta_numpy(previous, current, max_run: int, np) -> SequenceDelta:
    old = np.frombuffer(previous, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    new = np.frombuffer(current, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    changed = np.any(old != new, axis=1)
    return _build_sequence_delta_from_changed(new, changed, max_run, np)


def _suppress_small_changes_scalar(previous, current, threshold: int) -> bytes:
    out = bytearray(current)
    for offset in range(0, len(current), DEFAULT_BPP):
        if previous[offset + 3] != current[offset + 3]:
            continue
        if all(abs(previous[offset + channel] - current[offset + channel]) <= threshold
               for channel in range(3)):
            out[offset:offset + DEFAULT_BPP] = previous[offset:offset + DEFAULT_BPP]
    return bytes(out)


def _small_change_mask_numpy(old, new, threshold: int, np):
    changed = new[:, 3] != old[:, 3]
    for channel in range(3):
        difference = np.subtract(old[:, channel], new[:, channel], dtype=np.int16)
        np.abs(difference, out=difference)
        changed |= difference > threshold
    return changed


def _suppress_small_changes_numpy(previous, current, threshold: int, np) -> bytes:
    old = np.frombuffer(previous, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    new = np.frombuffer(current, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    changed = _small_change_mask_numpy(old, new, threshold, np)
    return np.where(changed[:, None], new, old).tobytes()


def suppress_small_changes(previous: bytes | bytearray,
                           current: bytes | bytearray,
                           threshold: int) -> bytes:
    """Keep displayed pixels whose BGR error is bounded by an explicit lossy threshold."""
    threshold = int(threshold)
    if not 0 <= threshold <= 255:
        raise ValueError("change threshold must be in 0..255")
    if len(previous) != len(current):
        raise ValueError(f"frame lengths differ: {len(previous)} != {len(current)}")
    if len(current) % DEFAULT_BPP:
        raise ValueError("sequence frames must contain whole BGRA pixels")
    if threshold == 0:
        return bytes(current)
    if len(current) >= 256 * DEFAULT_BPP:
        import image_encoder as ie
        np = ie._numpy()
        if np is not None:
            return _suppress_small_changes_numpy(previous, current, threshold, np)
    return _suppress_small_changes_scalar(previous, current, threshold)


def build_sequence_delta(previous: bytes | bytearray,
                         current: bytes | bytearray,
                         bpp: int = DEFAULT_BPP,
                         max_run: int = SEQUENCE_MAX_RUN) -> SequenceDelta:
    """Encode changed BGRA runs exactly as the ROM >= 1.89 vendor branch."""
    if bpp != DEFAULT_BPP:
        raise ValueError("the ROM >= 1.89 sequence path uses 4-byte BGRA pixels")
    if len(previous) != len(current):
        raise ValueError(f"frame lengths differ: {len(previous)} != {len(current)}")
    if len(current) % bpp:
        raise ValueError("sequence frames must contain whole BGRA pixels")
    if max_run < 1:
        raise ValueError("max_run must be >= 1")
    n_pixels = len(current) // bpp
    if n_pixels > 0x1000000:
        raise ValueError("pixel index does not fit in 24 bits")

    if n_pixels >= 256:
        import image_encoder as ie
        np = ie._numpy()
        if np is not None:
            return _build_sequence_delta_numpy(previous, current, max_run, np)
    return _build_sequence_delta_scalar(previous, current, max_run)


def _build_filtered_sequence_delta_numpy(previous, current, threshold: int,
                                         max_run: int, np):
    old = np.frombuffer(previous, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    new = np.frombuffer(current, dtype=np.uint8).reshape(-1, DEFAULT_BPP)
    changed = _small_change_mask_numpy(old, new, threshold, np)
    filtered = np.where(changed[:, None], new, old).tobytes()
    return _build_sequence_delta_from_changed(new, changed, max_run, np), filtered


def build_filtered_sequence_delta(previous: bytes | bytearray,
                                  current: bytes | bytearray,
                                  threshold: int,
                                  bpp: int = DEFAULT_BPP,
                                  max_run: int = SEQUENCE_MAX_RUN):
    """Build a delta and its filtered target from one changed-pixel mask when possible."""
    threshold = int(threshold)
    if not 0 <= threshold <= 255:
        raise ValueError("change threshold must be in 0..255")
    if threshold == 0:
        return build_sequence_delta(previous, current, bpp, max_run), bytes(current)
    if bpp != DEFAULT_BPP:
        raise ValueError("the ROM >= 1.89 sequence path uses 4-byte BGRA pixels")
    if len(previous) != len(current):
        raise ValueError(f"frame lengths differ: {len(previous)} != {len(current)}")
    if len(current) % bpp:
        raise ValueError("sequence frames must contain whole BGRA pixels")
    if max_run < 1:
        raise ValueError("max_run must be >= 1")
    if len(current) // bpp > 0x1000000:
        raise ValueError("pixel index does not fit in 24 bits")
    if len(current) >= 256 * DEFAULT_BPP:
        import image_encoder as ie
        np = ie._numpy()
        if np is not None:
            return _build_filtered_sequence_delta_numpy(
                previous, current, threshold, max_run, np)
    filtered = suppress_small_changes(previous, current, threshold)
    return build_sequence_delta(previous, filtered, bpp, max_run), filtered


def build_cmd204_frames(previous: bytes | bytearray,
                        current: bytes | bytearray,
                        sequence: int = 0,
                        allow_unverified: bool = False,
                        **kw) -> list[bytes]:
    """Build a visually verified cmd 204 delta, gated from implicit transmission."""
    if not allow_unverified:
        raise NotVerified(
            "cmd 204 is gated from implicit use. Pass allow_unverified=True only "
            "for a planned bounded transmit or offline inspection.")
    if not 0 <= int(sequence) <= 0xFFFFFFFF:
        raise ValueError("sequence must fit in 32 bits")

    delta = build_sequence_delta(previous, current, **kw)
    return build_cmd204_delta_frames(delta, sequence=sequence,
                                     allow_unverified=allow_unverified)


def build_cmd204_delta_frames(delta: SequenceDelta,
                              sequence: int = 0,
                              allow_unverified: bool = False) -> list[bytes]:
    """Frame an existing delta without comparing the source pixels a second time."""
    if not allow_unverified:
        raise NotVerified("cmd 204 transmission remains gated by explicit opt-in")
    if not 0 <= int(sequence) <= 0xFFFFFFFF:
        raise ValueError("sequence must fit in 32 bits")

    import image_encoder as ie
    import serial_proto as sp

    sequence_args = int(sequence).to_bytes(4, "big") + bytes(4)
    command = sp.build_frame(CMD_UPDATE_BITMAP, len(delta.payload), sequence_args, 0)
    return [command] + ie.iter_chunks(delta.payload)


def build_cmd204_delta_stream(delta: SequenceDelta,
                              sequence: int = 0,
                              allow_unverified: bool = False) -> bytes:
    """Build the same stream without allocating a bytes object for every chunk."""
    if not allow_unverified:
        raise NotVerified("cmd 204 transmission remains gated by explicit opt-in")
    if not 0 <= int(sequence) <= 0xFFFFFFFF:
        raise ValueError("sequence must fit in 32 bits")

    import image_encoder as ie
    import serial_proto as sp

    sequence_args = int(sequence).to_bytes(4, "big") + bytes(4)
    command = sp.build_frame(CMD_UPDATE_BITMAP, len(delta.payload), sequence_args, 0)
    return command + ie.pack_stream(delta.payload)


def apply_sequence_payload(previous: bytes | bytearray, payload: bytes) -> bytes:
    """Independently decode and apply a cmd 204 payload for offline verification."""
    if len(previous) % DEFAULT_BPP:
        raise ValueError("previous frame must contain whole BGRA pixels")
    if not payload.endswith(MAGIC):
        raise ValueError("sequence payload has no EF 69 terminator")
    body = payload[:-len(MAGIC)]
    if body == SEQUENCE_EMPTY:
        return bytes(previous)
    if not body:
        raise ValueError("an empty non-marker sequence body is invalid")

    result = bytearray(previous)
    cursor = 0
    while cursor < len(body):
        single = body[cursor] & 0x80
        header = 3 if single else SEQUENCE_RECORD_HEADER
        if cursor + header > len(body):
            raise ValueError("truncated sequence record header")
        index = int.from_bytes(body[cursor:cursor + 3], "big") & 0x7FFFFF
        if single:
            run = 1
        else:
            run = int.from_bytes(body[cursor + 3:cursor + 5], "big")
        cursor += header
        if run < 1:
            raise ValueError("sequence run length must be nonzero")
        size = run * DEFAULT_BPP
        if cursor + size > len(body):
            raise ValueError("truncated sequence pixels")
        start = index * DEFAULT_BPP
        if start + size > len(result):
            raise ValueError("sequence record exceeds the frame")
        result[start:start + size] = body[cursor:cursor + size]
        cursor += size
    return bytes(result)
