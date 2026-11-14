"""Image encoding and transport for the 6.5-inch serial (CH552 "rev-C") panel.

Implements the image path recovered from the vendor application.  Everything here
is byte-level reimplementation of decompiled code; **no hardware validation of the
image path has been done yet** (only HELLO has been verified on the real panel).

The public protocol summary is in docs/protocol.md.
--------------------------------------------------------
* `됒.cs:2725-2853`  frame builder `_A9B7(cmd, value, payload=null, extra=0)`
                     -> 250-byte frame, magic EF 69 at [1..2], value BE32 at [3..6]
* `됒.cs:3219-3220`  `됒.m_ꔂ = 249;  됒.m_ꗳ = 250;`
* `됒.cs:2855-2960`  chunked sender `_A9B7(byte[] data, int length)`:
                         chunk = new byte[250]
                         if (done + 249 >= length) copy the remainder
                         else                      copy exactly 249 bytes
                         done += 249
                         Write(chunk, 0, 250)
                     => payload is split into 249-byte pieces, each zero-extended
                        to a full 250-byte frame.
* `뽞.cs:6977-7070`  `ShowFullImage(byte[] data, int length, bool isDiiferInite)`:
                         if (isDiiferInite) _A9B7(202, data.Length, null, 0)   // 0xCA
                         else               _A9B7(200, data.Length, null, 0)   // 0xC8
                         _A9B7(data, length)                                    // chunks
                         if (isDiiferInite) SetPngPosInfo(data)
* `뽞.cs:6574-6658`  `SetPngPosInfo(byte[] img)`:
                         array = 깬.ꔂ(img, width, height, out A_)   // RLE mask
                         array[A_]=0xEF; array[A_+1]=0x69; A_ += 2  // append magic
                         _A9B7(208, A_, null, 0)                    // 0xD0
                         _A9B7(array, A_)
* `깬.cs:5412`       `ꔂ(byte[] data, int width, int height, out int len, int bpp=4,
                        int maxSize=65000)` -- the RLE mask encoder
* `깬.cs:4574-4703`  the run emitter (see `rle_mask`)
* `Monitor.cs:5226`  `currentFrameData = 깬.ꔂ(currentFrame, 4, screenDev.width_real)`
* `깬.cs:4462-4521`  `ꔂ(Bitmap, int bpp, int targetWidth=0)` -> resize then a `쐓`
                     buffer accessor with the requested bytes-per-pixel
* `쐓.cs`            `BitmapData` wrapper; `ꮙ(int bpp = 3)` sets bytes-per-pixel.
                     Contains NO bit packing (no masks, no shifts) -- it hands back
                     the raw buffer at the requested bpp.

PUBLIC-RE CROSS-CHECK
---------------------
Ten command numbers recovered from the vendor call sites match the independent
public reverse engineering of this family exactly:

    121 STOP_VIDEO, 123 SET_BRIGHTNESS, 125 OPTIONS, 131 SCREEN_OFF_ON,
    132 RESTART, 134 PRE_UPDATE_BITMAP, 150 STOP_MEDIA, 200 FULL_IMAGE,
    204 UPDATE_BITMAP, 207 QUERY_STATUS

Two commands present in our code are NOT in that public list:
    202 (0xCA) differ-init frame, 208 (0xD0) position/RLE mask.

DIVERGENCE FROM PUBLIC RE (recorded, per the operator's instruction to prefer our
own build's code and keep a difference report): the public notes mention the
changed-pixels command as `cc` (204) with a `ca` overlay variant; in OUR build the
differ-initialise path uses **202** and the mask path uses **208**. Both families
evidently have a 0xCA/0xCC pair; treat the numbers here as authoritative for this
panel.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from PIL import Image

import serial_proto as sp

# --------------------------------------------------------------------------
# Constants  [CONFIRMED] 되.cs:3219-3220
# --------------------------------------------------------------------------

CHUNK_PAYLOAD = 249
"""`됒.m_ꔂ = 249` -- payload bytes per frame."""

CHUNK_FRAME = sp.FRAME_SIZE
"""`됒.m_ꗳ = 250` -- every chunk is written as a full 250-byte frame."""

BYTES_PER_PIXEL = 4
"""`Monitor.cs:5226` -> `깬.ꔂ(currentFrame, 4, width_real)`; `쐓.ꮙ(4)`.

The accessor contains no bit packing, so `currentFrameData` is a RAW 4-bytes-per-
pixel buffer in the Bitmap's native order, i.e. **BGRA** for a 32bppArgb bitmap.
"""

RLE_RUN_BYTES = 6
"""Each run entry is 6 bytes in the emitter at `깬.cs:4677-4683`.

*** UNRESOLVED: this may not be the format cmd 208 actually uses. ***

`SetPngPosInfo` calls a DIFFERENT overload, `깬.ꔂ(byte[], int, int, out int, int=4,
int=65000)` at `깬.cs:5412`, and that one writes **10-byte** records (`num3 += 10`,
with a second `num3 += 5` path).  Both formats put a 24-bit big-endian pixel index
at [0..2]; they differ in what follows.  See reports/image-protocol-65inch.md
section 3.1.

Until the 10-byte layout is fully decoded, do not assume a mask built here is what
the device expects for cmd 208.
"""

RLE_MAX_COUNT = 15
"""The count lives in the high nibble of the 4th source byte, so 0..15 (>> 4)."""

RLE_SOURCE_LIMIT = 250
"""`깬.cs:4611` rejects a run byte > 250 (the low nibble must stay 0)."""

MASK_MAGIC = sp.MAGIC
"""`SetPngPosInfo` appends EF 69 to the end of the RLE mask (뽞.cs:6642-6645)."""


class ImageCmd:
    """Serial-family command numbers recovered from call sites."""

    FULL_IMAGE = 200       # 0xC8   뽞.cs:7002   value = pixel byte count, no payload
    DIFFER_INIT = 202      # 0xCA   뽞.cs:7043   value = pixel byte count, no payload
    SET_POS_INFO = 208     # 0xD0   뽞.cs:6646   value = mask byte count, payload = mask
    UPDATE_BITMAP = 204    # 0xCC   뽞.cs:6848   (public RE: changed pixels)
    QUERY_STATUS = 207     # 0xCF   뽞.cs:2369


# --------------------------------------------------------------------------
# Pixel encoding
# --------------------------------------------------------------------------

def to_bgra(image: Image.Image) -> bytes:
    """Convert to the raw BGRA byte order a 32bppArgb `Bitmap` exposes.

    PIL gives RGB; the vendor's buffer is the GDI+ 32bpp memory layout, which is
    B,G,R,A per pixel (little-endian 0xAARRGGBB).
    """
    # RGB中转保留既有语义：即使输入含透明度也强制线缆alpha=255。
    return image.convert("RGB").convert("RGBA").tobytes("raw", "BGRA")


def encode_pixels(image: Image.Image, bpp: int = BYTES_PER_PIXEL) -> bytes:
    """Produce the buffer the vendor sends as `currentFrameData`.

    Only `bpp == 4` (BGRA) is implemented, because that is the only value any
    recovered call site uses (`깬.ꔂ(frame, 4, width_real)`).
    """
    if bpp != 4:
        raise ValueError(
            f"bpp={bpp} is not used by any recovered call site; only 4 (BGRA) is "
            f"implemented. See the evidence map in this module."
        )
    return to_bgra(image)


def pixel_count(image: Image.Image) -> int:
    return image.width * image.height


def expected_pixel_bytes(width: int, height: int, bpp: int = BYTES_PER_PIXEL) -> int:
    return width * height * bpp


# --------------------------------------------------------------------------
# Chunking  [CONFIRMED] 됒.cs:2855-2960
# --------------------------------------------------------------------------

def iter_chunks(data: bytes, payload: int = CHUNK_PAYLOAD) -> list[bytes]:
    """Split `data` into 250-byte frames carrying `payload` (249) bytes each.

    The final frame is zero padded -- exactly what
    `array = new byte[250]` + partial `Array.Copy` produces.
    """
    if payload <= 0 or payload > CHUNK_FRAME:
        raise ValueError(f"payload must be 1..{CHUNK_FRAME}")
    frames = []
    off = 0
    total = len(data)
    while off < total:
        frame = bytearray(CHUNK_FRAME)
        n = total - off
        if n >= payload:
            frame[0:payload] = data[off:off + payload]
        else:
            frame[0:n] = data[off:off + n]
        off += payload
        frames.append(bytes(frame))
    return frames


def chunked_wire_size(data_len: int, payload: int = CHUNK_PAYLOAD) -> int:
    """Bytes actually put on the wire for `data_len` bytes of payload."""
    if data_len <= 0:
        return 0
    chunks = (data_len + payload - 1) // payload
    return chunks * CHUNK_FRAME


def iter_write_batches(frames: list[bytes], batch: int = 1) -> list[bytes]:
    """Group whole frames into larger `write()` calls.

    The device parses a **byte stream**, so the 250-byte frame boundaries are a
    property of the stream, not of the USB transfers -- concatenating frames into
    one `write()` is byte-for-byte identical on the wire and removes per-write
    syscall overhead.  Measured on hardware (2026-10-14, 720x1568 full frame):

        batch=1   -> 0.91 s   4.78 MiB/s
        batch=16  -> 0.25 s  17.24 MiB/s
        batch=64  -> 0.24 s  18.27 MiB/s
        batch=256 -> 0.23 s  18.75 MiB/s   (~4.3 fps)

    Never splits a frame: each returned item is a whole number of 250-byte frames.

    NOTE: `plan_stream()` + `iter_batch_slices()` is the faster equivalent and is
    what `SamaScreen.show_image()` uses.  This function is kept because it is the
    API the verified tests exercise, and the two are asserted to agree.
    """
    batch = max(1, int(batch))
    if batch == 1:
        return list(frames)
    return [b"".join(frames[i:i + batch]) for i in range(0, len(frames), batch)]


# --------------------------------------------------------------------------
# Fast path: build the whole wire stream in one vectorised step
# --------------------------------------------------------------------------
# Profiling on the real 720x1568 frame showed the per-frame object construction
# was pure overhead: `iter_chunks` spent 7.9 ms creating 18,136 separate 250-byte
# `bytes` objects, and batching is just a slice of the concatenation anyway.
#
# The framing rule is trivially vectorisable: a zero byte every 250, payload in
# the other 249 slots.  So build the whole stream at once.

_np = None


def _numpy():
    """Import numpy lazily; return None if unavailable."""
    global _np
    if _np is None:
        try:
            import numpy
            _np = numpy
        except ImportError:                                   # pragma: no cover
            _np = False
    return _np or None


def pack_stream(data: bytes, payload: int = CHUNK_PAYLOAD) -> bytes:
    """Pack `data` into 250-byte frames WITHOUT creating one object per frame.

    Byte-identical to ``b"".join(iter_chunks(data))`` -- asserted in the tests.

    Implementation of the framing rule from `됒.cs:2855-2960`:
      * payload bytes go into slots 0..248 of each frame,
      * slot 249 of every frame is zero,
      * the final frame is zero padded.
    """
    if payload <= 0 or payload > CHUNK_FRAME:
        raise ValueError(f"payload must be 1..{CHUNK_FRAME}")
    n = len(data)
    if n == 0:
        return b""

    np = _numpy()
    if np is not None and n > 4 * CHUNK_FRAME:
        chunks = (n + payload - 1) // payload
        out = np.zeros((chunks, CHUNK_FRAME), dtype=np.uint8)
        _fill_stream_rows(out, data, payload, np)
        return out.tobytes()

    # pure-python fallback (also the reference implementation for the tests)
    return b"".join(iter_chunks(data, payload))


def _fill_stream_rows(rows, data, payload, np):
    """直接把完整负载行及尾部复制到零初始化输出，省掉全幅padding副本。"""
    full, tail = divmod(len(data), payload)
    source = np.frombuffer(data, dtype=np.uint8)
    if full:
        rows[:full, :payload] = source[:full * payload].reshape(full, payload)
    if tail:
        rows[full, :tail] = source[full * payload:]


def plan_stream(plan: "FramePlan") -> bytes:
    """The complete wire byte stream for a plan: command frame, then the payload.

    Replaces ``b"".join(plan.all_frames())``.  Byte-identical, asserted in tests.
    """
    np = _numpy()
    if np is not None and len(plan.payload) > 4 * CHUNK_FRAME:
        chunks = (len(plan.payload) + CHUNK_PAYLOAD - 1) // CHUNK_PAYLOAD
        trailing = (len(plan.trailing_payload) + CHUNK_PAYLOAD - 1) // CHUNK_PAYLOAD
        # 一个连续输出承载命令、像素、可选mask，避免pack→bytearray→bytes重复拼接。
        out = np.zeros((1 + chunks + (1 + trailing if trailing else 0), CHUNK_FRAME), dtype=np.uint8)
        out[0] = np.frombuffer(plan.command_frame, dtype=np.uint8)
        _fill_stream_rows(out[1:1 + chunks], plan.payload, CHUNK_PAYLOAD, np)
        if trailing:
            out[1 + chunks] = np.frombuffer(plan.trailing_command, dtype=np.uint8)
            _fill_stream_rows(out[2 + chunks:], plan.trailing_payload, CHUNK_PAYLOAD, np)
        return out.tobytes()
    out = bytearray(plan.command_frame)
    out += pack_stream(plan.payload)
    if plan.trailing_payload:
        out += plan.trailing_command
        out += pack_stream(plan.trailing_payload)
    return bytes(out)


def iter_batch_slices(stream: bytes, batch: int = 256) -> list[bytes]:
    """Slice an already-built wire stream into `batch`-frame write groups.

    `batch` counts 250-byte FRAMES, so one slice is `batch * 250` bytes and is
    always a whole number of frames.  Measured cost for the full 720x1568 frame:
    1.4 ms -> effectively free compared with building the frames individually.
    """
    batch = max(1, int(batch))
    step = batch * CHUNK_FRAME
    return [stream[i:i + step] for i in range(0, len(stream), step)]


# --------------------------------------------------------------------------
# Sequences
# --------------------------------------------------------------------------

@dataclass
class FramePlan:
    """A fully built image transfer, ready to hand to a transport.

    `data_frames` / `trailing_frames` are the per-frame objects (kept because the
    verified tests exercise them).  `payload` / `trailing_payload` hold the RAW
    bytes, which is what the fast path packs from -- packing raw bytes is far
    cheaper than creating ~18,000 small objects.
    """

    command_frame: bytes
    data_frames: list[bytes] = field(default_factory=list)
    trailing_frames: list[bytes] = field(default_factory=list)
    pixel_bytes: int = 0
    wire_bytes: int = 0
    mode: str = "full"
    payload: bytes = b""
    trailing_payload: bytes = b""
    trailing_command: bytes = b""

    def all_frames(self) -> list[bytes]:
        return [self.command_frame] + self.data_frames + self.trailing_frames

    def described(self) -> str:
        return (f"mode={self.mode} pixel_bytes={self.pixel_bytes:,} "
                f"frames={1 + len(self.data_frames) + len(self.trailing_frames)} "
                f"wire_bytes={self.wire_bytes:,}")


def build_full_image(pixels: bytes, frames: bool = True) -> FramePlan:
    """cmd 200 + chunked raw pixels.  [CONFIRMED] 뽞.cs:6990-7040.

    `frames=False` skips building the per-frame objects entirely.  That is the
    point of the fast path: creating ~18,000 small `bytes` objects costs ~8 ms and
    is pure overhead when all we do is concatenate them again.  With
    `frames=False`, use `plan_stream(plan)` -- `plan.all_frames()` will be EMPTY
    and must not be used.
    """
    cmd = sp.build_frame(ImageCmd.FULL_IMAGE, len(pixels), b"", 0)
    chunks = iter_chunks(pixels) if frames else []
    return FramePlan(command_frame=cmd, data_frames=chunks,
                     pixel_bytes=len(pixels),
                     wire_bytes=len(cmd) + chunked_wire_size(len(pixels)),
                     mode="full", payload=pixels)


def build_differ_init(pixels: bytes, mask: bytes | None = None,
                      frames: bool = True) -> FramePlan:
    """cmd 202 + chunked pixels (+ optional cmd 208 mask).  [CONFIRMED] 뽞.cs:7041-7054.

    NOTE (see reports/transport-bottleneck-analysis.md section 3.3): this does NOT
    save bandwidth.  Both ShowFullImage branches stream the SAME full pixel buffer;
    the differ path merely adds the position list on top.  It is a device-side
    partial-refresh hint, not differential compression.
    """
    cmd = sp.build_frame(ImageCmd.DIFFER_INIT, len(pixels), b"", 0)
    data = iter_chunks(pixels) if frames else []
    trailing: list[bytes] = []
    extra = 0
    pos_cmd = b""
    if mask:
        pos_cmd = sp.build_frame(ImageCmd.SET_POS_INFO, len(mask), b"", 0)
        trailing = [pos_cmd] + (iter_chunks(mask) if frames else [])
        extra = len(pos_cmd) + chunked_wire_size(len(mask))
    return FramePlan(command_frame=cmd, data_frames=data, trailing_frames=trailing,
                     pixel_bytes=len(pixels),
                     wire_bytes=len(cmd) + chunked_wire_size(len(pixels)) + extra,
                     mode="differ-init", payload=pixels,
                     trailing_payload=mask or b"", trailing_command=pos_cmd)


# --------------------------------------------------------------------------
# RLE mask  [CONFIRMED] 깬.cs:4574-4703 (emitter), 5412 (entry point)
# --------------------------------------------------------------------------

def rle_mask(bgra: bytes, counts: bytes | None = None) -> bytes:
    """Build the 6-bytes-per-run sparse mask.

    Faithful reimplementation of the emitter at `깬.cs:4675-4683`::

        count = src[p*4 + 3] >> 4
        out[n+0..2] = p  as 24-bit BIG-endian            (absolute pixel index)
        out[n+3]    = (src[p*4+0] & 0xFC) | (count >> 2) (B top 6 bits + count 2 MSB)
        out[n+4]    = (src[p*4+1] & 0xFC) | (count &  3) (G top 6 bits + count 2 LSB)
        out[n+5]    =  src[p*4+2]                        (R, full 8 bits)
        n += 6

    A run is emitted only where the 4th byte is non-zero and <= 250
    (`깬.cs:4611`, `4621`).

    Parameters
    ----------
    bgra
        The same 4-bytes-per-pixel buffer that was sent as the frame.
    counts
        Per-pixel run length (1..15).  **Strongly recommended**: if omitted the
        count is taken from `bgra`'s 4th byte, which for a normal image is the
        alpha value 255 -- and 255 > `RLE_SOURCE_LIMIT`, so every run is skipped
        and the mask comes out empty.  That is faithful to the vendor emitter,
        which is exactly why the vendor computes the mask from a frame difference
        (see `깬.ꔂ(byte[], int, int, out int, int=4, int=65000)` at `깬.cs:5412`)
        rather than from a plain frame.  Pass explicit `counts`, or use
        :func:`plan_image` with ``mode="differ-init"`` which supplies a default.

    Note the colour channel order on the wire is **B6|G6|R8** with the 4-bit run
    count interleaved into the low 2 bits of B and G.  The low 2 bits of B and G
    are therefore LOST (masked with 0xFC) -- colour is quantised to 6 bits for B
    and G and 8 bits for R.
    """
    if len(bgra) % 4:
        raise ValueError("expected a 4-bytes-per-pixel buffer")

    src = bgra
    if counts is not None:
        if len(counts) != len(bgra) // 4:
            raise ValueError("counts must have one entry per pixel")
        src = bytearray(bgra)
        for i, c in enumerate(counts):
            src[i * 4 + 3] = (int(c) & 0x0F) << 4
        src = bytes(src)

    out = bytearray()
    for p in range(len(src) // 4):
        b = src[p * 4 + 0]
        g = src[p * 4 + 1]
        r = src[p * 4 + 2]
        a = src[p * 4 + 3]
        if a == 0 or a > RLE_SOURCE_LIMIT:
            continue
        count = a >> 4
        out += bytes(((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF,
                      (b & 0xFC) | ((count >> 2) & 0x03),
                      (g & 0xFC) | (count & 0x03),
                      r))
    return bytes(out)


def decode_rle_mask(mask: bytes) -> list[dict]:
    """Inverse of :func:`rle_mask`, for tests and for inspecting device traffic."""
    if len(mask) % RLE_RUN_BYTES:
        raise ValueError(f"mask length {len(mask)} is not a multiple of {RLE_RUN_BYTES}")
    runs = []
    for i in range(0, len(mask), RLE_RUN_BYTES):
        idx = (mask[i] << 16) | (mask[i + 1] << 8) | mask[i + 2]
        b6 = mask[i + 3] & 0xFC
        g6 = mask[i + 4] & 0xFC
        count = ((mask[i + 3] & 0x03) << 2) | (mask[i + 4] & 0x03)
        runs.append({"index": idx, "b": b6, "g": g6, "r": mask[i + 5], "count": count})
    return runs


def build_mask_payload(mask: bytes) -> bytes:
    """`SetPngPosInfo` appends the EF 69 magic to the mask.  [CONFIRMED] 뽞.cs:6642-6645."""
    return bytes(mask) + MASK_MAGIC


# --------------------------------------------------------------------------
# High level
# --------------------------------------------------------------------------

def plan_image(image: Image.Image, mode: str = "full",
               counts: bytes | None = None, frames: bool = True) -> FramePlan:
    """Encode `image` and build the frame plan.

    `mode` is ``"full"`` (cmd 200) or ``"differ-init"`` (cmd 202 + cmd 208 mask).
    `frames=False` skips per-frame object construction; pair it with
    `plan_stream(plan)` -- see `build_full_image`.

    For ``"differ-init"`` a `counts` array (one run length 1..15 per pixel) may be
    supplied.  If it is omitted, a **conservative, uncompressed** default is used:
    one run per pixel with count 1.  That is a valid mask, but it does not
    reproduce the vendor's run merging, which is computed from a frame difference
    at `깬.ꔂ(byte[], int, int, out int, int=4, int=65000)` (`깬.cs:5412`) and is
    **not yet replicated** -- so differ mode is expected to be larger and slower
    than the vendor's until that is implemented.
    """
    pixels = encode_pixels(image)
    if mode == "full":
        return build_full_image(pixels, frames=frames)
    if mode == "differ-init":
        n_pixels = len(pixels) // 4
        if counts is None:
            counts = bytes([1]) * n_pixels
        if len(counts) != n_pixels:
            raise ValueError(f"counts must have {n_pixels} entries")
        mask = build_mask_payload(rle_mask(pixels, counts))
        return build_differ_init(pixels, mask, frames=frames)
    raise ValueError(f"unknown mode {mode!r}; expected 'full' or 'differ-init'")


def send_plan(transport, plan: FramePlan, safety=None, pause_s: float = 0.0,
              on_frame=None) -> int:
    """Write every frame of `plan` to `transport`.  Returns the frames written.

    Requires `safety.WriteClass.IMAGE` when a Safety object is supplied.  An
    optional `pause_s` sleeps between frames (the vendor has a 1 ms sleep in its
    chunk loop, `됒.cs:2925`).
    """
    import time
    if safety is not None:
        from safety import WriteClass
        safety.require(WriteClass.IMAGE, f"image {plan.mode} "
                                        f"{plan.pixel_bytes:,} B")
    n = 0
    for f in plan.all_frames():
        transport.write(f)
        n += 1
        if on_frame:
            on_frame(n, f)
        if pause_s:
            time.sleep(pause_s)
    return n


def guard_family(transport_name: str) -> None:
    """Refuse to send this protocol over a non-serial transport.

    The 0x1CBE/WinUSB family uses DES-framed 512-byte packets; sending serial
    250-byte frames there (or vice versa) is a protocol-family error.
    """
    if "serial" not in transport_name.lower() and "mock" not in transport_name.lower():
        raise ValueError(
            f"the 250-byte serial frames are for the CDC-ACM family only; "
            f"refusing to send them over {transport_name!r}. The 0x1CBE family uses "
            f"DES-framed 512-byte packets (see protocol.py)."
        )
