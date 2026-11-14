"""Serial (CDC-ACM) protocol for the CH552 / "rev-C" panel family.

This is the protocol spoken by the panel that enumerates as ``1D6B:A065``
("Gadget Serial v2.4", Linux Foundation VID) plus its ``1A86:CA65`` CH552 wake
MCU -- i.e. the 6.5-inch 720x1568 unit.  It is **NOT** the DES-framed 0x1CBE
protocol implemented in `protocol.py`; that one belongs to a different transport
family and must never be sent to a serial panel.

FRAME LAYOUT -- fully recovered from the vendor code
---------------------------------------------------
`됒._A9B7(int cmd, int value, byte[] payload = null, byte extra = 0)`
at `됒.cs:2725-2853`, writing through `SerialPortStream.Write` (`됒.cs:3026-3135`):

    byte[] array = new byte[됒.m_ꗳ];        // m_ꗳ = 250  (됒.cs:3220)
    int num4 = 61289;                       // == 0xEF69   <- the magic
    array[0] = (byte)cmd;
    array[1] = (byte)(num4 >> 8);           // 0xEF
    array[2] = (byte)(num4 & 0xFF);         // 0x69
    array[3..6] = value as BIG-ENDIAN uint32
    array[7] = extra;
    // array[8] and array[9] are NEVER assigned -> stay 0x00
    if (payload != null) Array.Copy(payload, 0, array, 10, payload.Length);
    Write(array, 0, 250);

Properties of note:

* **Fixed 250-byte frame**, zero padded.  Matches the public reverse engineering
  of this family ("all messages padded to a multiple of 250 bytes").
* **No checksum, no CRC, no terminator** -- a project-wide search of `됒.cs`
  finds no CRC/checksum computation at all.
* **No encryption.**  `뽞.cs` (the class that drives this panel) has no crypto:
  no `DESCryptoServiceProvider`, no `TuzEnc`.
* Payload always starts at offset **10**, leaving a permanent 2-byte hole at
  [8..9].

KNOWN COMMANDS (from call sites)
--------------------------------
    1     HELLO / version request      뽞.cs:446   payload {0xC5, 0xD3}
    110   (file/asset transfer)        뽞.cs:902
    123   SET_BRIGHTNESS  (0x7B)       뽞.cs:7921  payload {level}
    129   SET_ROTATE      (0x81)       뽞.cs:7943  payload {r}

Cmd 123 == 0x7B matches the independently published reverse engineering of this
family ("7b = SET_BRIGHTNESS"), and the HELLO frame we build here matches the
published HELLO byte-for-byte -- two independent sources agreeing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Frame geometry  [CONFIRMED] 됒.cs:2725-2853, 3026-3135, 3220
# --------------------------------------------------------------------------

FRAME_SIZE = 250
"""`됒.m_ꗳ = 250` (됒.cs:3220)."""

MAGIC = bytes((0xEF, 0x69))
"""0xEF69 == 61289, the literal assigned at 됒.cs:2795."""

MAGIC_WORD = 61289
PAYLOAD_OFFSET = 10
"""Payload starts at offset 10; offsets 8..9 are never written by the vendor."""

VALUE_OFFSET = 3
EXTRA_OFFSET = 7
HOLE_OFFSET = 8

#: Serial parameters the vendor uses (뽞.cs:8356-8361, 됒.cs:1509-1514).
SERIAL_BAUD = 115200
SERIAL_DATABITS = 8
SERIAL_PARITY = "N"
SERIAL_STOPBITS = 1
SERIAL_DTR = True
SERIAL_RTS = True

#: Reply framing: the published RE of this family says replies are padded to
#: 1024 bytes.  Not yet confirmed from the vendor code, so treated as a read hint.
REPLY_SIZE_HINT = 1024


class SerialCmd:
    """Command bytes recovered from call sites (not from an enum -- the vendor's
    `눰` enum at 눰.cs holds a *different* value set and is not this command
    space)."""

    HELLO = 1
    SET_BRIGHTNESS = 123       # 0x7B
    SET_ROTATE = 129           # 0x81
    TRANSFER = 110
    OPTIONS = 0x7D             # 125  -- published RE
    STOP_VIDEO = 0x79          # 121  -- published RE
    STOP_MEDIA = 0x96          # 150  -- published RE
    RESTART = 0x84             # 132  -- published RE
    SCREEN_OFF_ON = 0x83       # 131  -- published RE
    PRE_UPDATE_BITMAP = 0x86   # 134  -- published RE
    FULL_IMAGE = 0xC8          # 200  -- published RE
    UPDATE_BITMAP = 0xCC       # 204  -- published RE
    QUERY_STATUS = 0xCF        # 207  -- published RE
    SEND_PAYLOAD = 0xFF        # 255  -- published RE


@dataclass
class SerialFrame:
    cmd: int
    value: int
    extra: int
    payload: bytes
    raw: bytes

    @property
    def is_hello(self) -> bool:
        return self.cmd == SerialCmd.HELLO

    def describe(self) -> str:
        return (f"cmd={self.cmd} (0x{self.cmd:02X}) value={self.value} "
                f"extra={self.extra} payload={len(self.payload)}B "
                f"frame={len(self.raw)}B")


# --------------------------------------------------------------------------
# Build / parse
# --------------------------------------------------------------------------

def build_frame(cmd: int, value: int, payload: bytes = b"", extra: int = 0) -> bytes:
    """Build one 250-byte serial frame.  Byte-exact reimplementation of `됒.cs:2725`.

    Raises ValueError if the payload cannot fit before the 250-byte boundary.
    """
    if not 0 <= int(cmd) <= 0xFF:
        raise ValueError("cmd must fit in one byte")
    if not 0 <= int(extra) <= 0xFF:
        raise ValueError("extra must fit in one byte")
    if not 0 <= int(value) <= 0xFFFFFFFF:
        raise ValueError("value must fit in a uint32")
    payload = bytes(payload or b"")
    if PAYLOAD_OFFSET + len(payload) > FRAME_SIZE:
        raise ValueError(
            f"payload of {len(payload)} B does not fit: offset {PAYLOAD_OFFSET} "
            f"+ payload must be <= {FRAME_SIZE}"
        )

    frame = bytearray(FRAME_SIZE)                 # zero padded by construction
    frame[0] = int(cmd) & 0xFF
    frame[1] = (MAGIC_WORD >> 8) & 0xFF           # 0xEF
    frame[2] = MAGIC_WORD & 0xFF                  # 0x69
    frame[VALUE_OFFSET:VALUE_OFFSET + 4] = struct.pack(">I", int(value) & 0xFFFFFFFF)
    frame[EXTRA_OFFSET] = int(extra) & 0xFF
    # frame[HOLE_OFFSET:HOLE_OFFSET + 2] deliberately left 0x00, as the vendor does
    if payload:
        frame[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(payload)] = payload
    return bytes(frame)


def parse_frame(data: bytes) -> SerialFrame:
    """Decode a frame produced by :func:`build_frame` (or read from the wire)."""
    if len(data) < PAYLOAD_OFFSET:
        raise ValueError(f"frame too short: {len(data)}")
    if data[1:3] != MAGIC:
        raise ValueError(f"bad magic: {data[1:3].hex()} (expected {MAGIC.hex()})")
    value = struct.unpack(">I", data[VALUE_OFFSET:VALUE_OFFSET + 4])[0]
    body = data[PAYLOAD_OFFSET:FRAME_SIZE] if len(data) >= FRAME_SIZE \
        else data[PAYLOAD_OFFSET:]
    return SerialFrame(cmd=data[0], value=value, extra=data[EXTRA_OFFSET],
                       payload=body, raw=bytes(data[:FRAME_SIZE]))


# --------------------------------------------------------------------------
# The vendor's own handshake
# --------------------------------------------------------------------------

HELLO_PAYLOAD = bytes((0xC5, 0xD3))
"""`뽞.cs:445` -> `_A9B7 = new byte[2] { 197, 211 };` then `_A9B7(1, 1, _A9B7, 0)`
at `뽞.cs:446`."""


def build_hello() -> bytes:
    """The exact HELLO the vendor sends first.

    Reproduces `뽞.cs:446` `_A9B7(1, 1, {0xC5,0xD3}, 0)`, which yields::

        01 EF 69 00 00 00 01 00 00 00 C5 D3 <238 zero bytes>

    This matches, byte for byte, the HELLO documented by the public reverse
    engineering of this family (`01 ef 69 00 00 00 01 00 00 00 c5 d3`).
    """
    return build_frame(SerialCmd.HELLO, 1, HELLO_PAYLOAD, 0)


def build_brightness(level: int) -> bytes:
    """`뽞.cs:7920-7921` -- `_A9B7(123, 1, {level}, 0)`."""
    if not 0 <= level <= 0xFF:
        raise ValueError("brightness must be one byte")
    return build_frame(SerialCmd.SET_BRIGHTNESS, 1, bytes((level,)), 0)


def build_rotate(value: int) -> bytes:
    """`뽞.cs:7942-7943` -- `_A9B7(129, 1, {r}, 0)`."""
    if not 0 <= value <= 0xFF:
        raise ValueError("rotation must be one byte")
    return build_frame(SerialCmd.SET_ROTATE, 1, bytes((value,)), 0)


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------

def decode_reply(data: bytes) -> dict:
    """Best-effort decode of a reply.

    The published RE of this family says the device answers HELLO with an ASCII
    identity string such as ``chs_5inch.dev1_rom1.88``.  The panel in hand is a
    6.5-inch unit, so ``chs_65inch.dev1_rom<X.YZ>`` is the expectation -- but this
    has NOT been observed on hardware yet.
    """
    if not data:
        return {"raw_len": 0, "text": "", "identity": None, "version": None}

    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    runs = [r for r in _ascii_runs(data) if len(r) >= 3]
    identity = max(runs, key=len) if runs else None
    version = None
    if identity:
        tail = identity.split("_")[-1]
        for prefix in ("rom",):
            if tail.startswith(prefix):
                tail = tail[len(prefix):]
        try:
            version = float(tail)
        except ValueError:
            version = None
    return {
        "raw_len": len(data),
        "text": printable,
        "identity": identity,
        "version": version,
        "frames": _split_frames(data),
    }


def _ascii_runs(data: bytes):
    out, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if cur:
                out.append(cur.decode("ascii"))
                cur = bytearray()
    if cur:
        out.append(cur.decode("ascii"))
    return out


def _split_frames(data: bytes) -> int:
    """Count how many 250-byte frames the reply contains (padded, so >= 1)."""
    if not data:
        return 0
    return max(1, (len(data) + FRAME_SIZE - 1) // FRAME_SIZE)
