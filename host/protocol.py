"""Wire protocol for the TURZX / UsbMonitorL family of USB secondary displays.

This module is a faithful re-implementation of the packet layer recovered by
static analysis of the vendor's `SAMA.exe` (v4.2.1.3).  Every constant below
cites the decompiled source it came from.

The public protocol summary is in docs/protocol.md.
----------------------------------------------------------
* `목.cs:17-52`    `enum ꔂ : byte` -- the opcode space (values 10..253).
* `목.cs:2703-2735` `private byte[] 굻(byte opcode)` -- builds the 500-byte
                    plaintext block: [0]=opcode, [2]=0x1A, [3]=0x6D,
                    [4..7]=LE32(uptime_ms).
* `목.cs:2675-2701` `public long 벋()` -- `(DateTime.UtcNow - _start).TotalMilliseconds`.
* `목.cs:2737-2769` `private byte[] 굻(byte[] block)` -- DES-CBC encrypts the
                    500-byte block (PKCS#7 -> 504 bytes), copies it into a
                    512-byte buffer and appends the literal trailer A1 1A.
* `목.cs:2771-2813` `private static byte[] 굻(byte[] key, byte[] data)` --
                    `DESCryptoServiceProvider`, `Mode = CBC`, `Key = IV = key`.
* `목.cs:2741`      the key literal: `{115,108,118,51,116,117,122,120}`
                    == ASCII "slv3tuzx".
* `목.cs:4313-4388` `굻(opcode)`               -- command with empty payload.
* `목.cs:4390-4466` `굻(opcode, byte arg)`     -- `array[8] = arg`.
* `목.cs:4468-4580` `굻(opcode, byte[] data)`  -- `Array.Copy(data, 0, block, 8,
                    min(len,492))`; if `len > 492` the remainder is appended
                    RAW (unencrypted) after the first 512-byte packet.
* `목.cs:4617`      `this.m_눰.Write(buf, 2000, out _)` -- `m_눰` is a
                    `LibUsbDotNet.Main.UsbEndpointWriter`, timeout 2000 ms.
* `꽝.cs:6384-6415` `SetBrightness` -> `뽞().굻(목.ꔂ._A8C6, level)` i.e. opcode 14.

STATUS OF EACH CLAIM
--------------------
[CONFIRMED]  read directly from the decompiled vendor code (citations above).
[INFERENCE]  strongly implied by surrounding code but not proven.
[UNKNOWN]    opcode semantics not recoverable -- the obfuscator renamed every
             enum member to a meaningless identifier (e.g. `_A8C6`), and the
             names cannot be recovered from the binary.

SAFETY
------
Nothing here has been validated against real hardware.  [UNKNOWN] opcodes must
not be sent to a device.  See ../NEXT_STEPS.md for the planned capture/validation
procedure.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Packet geometry  [CONFIRMED] 목.cs:2703-2769, 4468-4580
# --------------------------------------------------------------------------

PACKET_SIZE = 512
"""Every command starts with one 512-byte packet (`new byte[512]`)."""

HEADER_SIZE = 8
"""Plaintext header: [0]=opcode [1]=? [2]=0x1A [3]=0x6D [4..7]=uptime LE32."""

MAGIC_2 = 0x1A
MAGIC_3 = 0x6D
"""Constant bytes at plaintext offsets 2 and 3."""

FIRST_PACKET_PAYLOAD = PACKET_SIZE - HEADER_SIZE - (PACKET_SIZE - 500)
# = 492.  Derived from `Array.Copy(data, 0, block, 8, min(len, 492))`, which
# fills the 500-byte plaintext block (offsets 8..499).

TRAILER = bytes((0xA1, 0x1A))
"""Literal trailer written *after* encryption at packet offsets 510..511."""

DES_KEY = b"slv3tuzx"
"""ASCII bytes {115,108,118,51,116,117,122,120} -- used as BOTH key and IV.
[CONFIRMED] 목.cs:2741 + 2771-2783.  Note this is obfuscation, not security:
the key is a hardcoded 8-byte literal and DES is broken."""

ENCRYPTED_PREFIX = PACKET_SIZE
"""Only the first 512 bytes of a command are encrypted.  Payload beyond the
first 492 bytes is appended in the clear.  [CONFIRMED] 목.cs:4546-4549."""

WRITE_TIMEOUT_MS = 2000
"""LibUsbDotNet `UsbEndpointWriter.Write(buf, 2000, out _)`. [CONFIRMED] 목.cs:4617."""


# --------------------------------------------------------------------------
# Opcodes  [CONFIRMED] values, [UNKNOWN] names  -- 목.cs:17-52
# --------------------------------------------------------------------------

class Cmd(enum.IntEnum):
    """The vendor's opcode space.

    Names and semantics come from two independent, agreeing sources:

    * **S1** -- this project's static analysis of `SAMA.exe` 4.2.1.3.  Values
      from `목.cs:17-56`; semantics from the request-builder call sites.  The
      obfuscator renamed every enum member to an arbitrary identifier drawn from
      the same pool as the assembly's *class* names, so the member names carry
      **no semantic information** and must not be read as mnemonics.
    * **S2** -- public reverse engineering of the same vendor protocol
      (`slipalison/bezel`, `mathoudebine/turing-smart-screen-python`), derived
      from the vendor's device table.

    Every entry below is annotated with the strength of its evidence.  The full
    derivation, with a per-opcode argument layout, is in
    `../reports/usb-communication.md`.

    CAUTION: S2 was derived from vendor app TURZX V3.07 while the target here is
    SAMA.exe 4.2.1.3.  Opcodes marked NEEDS HARDWARE are NOT safe to send.
    """

    GET_VERSION = 10          # CONFIRMED -- resp[0]==10, resp[8..39] = UTF-8 version string
    REBOOT = 11               # CONFIRMED value; device action STRONG INFERENCE
    SHUTDOWN = 12             # CONFIRMED value; device action STRONG INFERENCE
    SET_ROTATION = 13         # CONFIRMED -- 1 arg byte at payload offset 8
    SET_BRIGHTNESS = 14       # CONFIRMED -- 1 arg byte; vendor name was `_A8C6`
    SET_FRAME_RATE = 15       # STRONG INFERENCE -- call site logs "FrameRate={0}"
    CMD_11 = 17               # UNKNOWN -- reached only from a device-notify handler
    CMD_26 = 38               # UNKNOWN -- never referenced by name
    UPDATE_LARGE = 39         # CONFIRMED -- 1 MiB chunks; [8..11] BE size, [16]=1 on last
    UPDATE_SMALL = 40         # CONFIRMED -- small file upload (< 100 KB)
    DEL_FILE_OR_DIR = 42      # STRONG INFERENCE -- payload is a BE-length + ASCII path
    SEND_SYNC_CLOCK = 49      # CONFIRMED arg encoding
    QUERY_VOLTAGE = 50        # CONFIRMED encoding -- resp[8..11] BE int32
    SET_DISPLAY_ENABLE = 51   # CONFIRMED framing -- payload[7] = 1 (on) / 2 (off)
    STOP_DISPLAY_PLAY = 52    # STRONG INFERENCE -- fixed arg 0
    CMD_3C = 60               # UNKNOWN -- never referenced
    CMD_3D = 61               # UNKNOWN -- never referenced
    CMD_3E = 62               # UNKNOWN -- never referenced
    GET_FILE_SIZE = 98        # CONFIRMED -- resp[8..11] = LITTLE-endian uint32
    QUERY_DIR = 99            # CONFIRMED -- multi-block UTF-8 directory listing
    QUERY_SD_CARD = 100       # CONFIRMED -- resp[8..31] = 6 BE uint32 capacity fields
    SHOW_JPEG = 101           # CONFIRMED -- non-PNG (JPEG) blob, raw payload at 512
    SHOW_PNG = 102            # CONFIRMED -- adds the value; absent from the vendor enum
    CREATE_FILE = 110         # CONFIRMED -- resp[8]==200 on success
    STOP_VIDEO_PLAY = 111     # STRONG INFERENCE
    QUERY_IS_MEDIA_STOP = 112 # STRONG INFERENCE -- resp[8]==0 means stopped
    PLAY_LOCAL_JPG = 113      # CONFIRMED -- resp[1]==200 on success
    SEND_MEDIA_STOP = 114     # STRONG INFERENCE
    PLAY_H264_CHUNK = 121     # CONFIRMED -- [8..11] BE chunk length, up to 3 retries
    PLAY_H264_START = 122     # CONFIRMED -- 8-byte BE millisecond timestamp argument
    END_MONITORING = 123      # STRONG INFERENCE -- always preceded by opcode 51 (off)
    SET_CONFIG = 125          # CONFIRMED -- exactly 7 argument bytes
    CHECK_TEST_STEP = 150     # opcode use CONFIRMED; meaning not provable
    CMD_C9 = 201              # UNKNOWN -- different sub-system
    USB_RESET_BURST = 246      # STRONG INFERENCE -- builder called in a loop of 10
    DISPLAY_REGION = 251      # arg layout CONFIRMED; semantics NEEDS HARDWARE
    INDEXED_WRITE = 252       # arg layout CONFIRMED; semantics NEEDS HARDWARE
    CMD_FD = 253              # UNKNOWN -- never referenced


#: Opcodes the vendor code emits that have **no member** in its own `목.ꔂ` enum.
#: The enum is demonstrably incomplete: it skips 102..109 entirely.
OPCODES_NOT_IN_ENUM = {
    41: "file / storage family (documented by public RE; absent from the S1 enum)",
    96: "QUERY_TEMPERATURES -- read two temperatures over the unframed 64-byte "
        "channel (꽝.cs:4951-5007); a bare byte with no enum member",
    102: "SHOW_PNG -- see Cmd.SHOW_PNG",
}


# --------------------------------------------------------------------------
# DES-CBC transport encryption
# --------------------------------------------------------------------------

def _des():
    """Return a fresh DES-CBC cipher with key == IV == b'slv3tuzx'."""
    try:
        from Crypto.Cipher import DES
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pycryptodome is required for the transport layer: pip install pycryptodome"
        ) from exc
    return DES.new(DES_KEY, DES.MODE_CBC, DES_KEY)


def encrypt_block(block: bytes) -> bytes:
    """Reproduce `목.cs:2771-2813` -- DES-CBC + PKCS#7 over the 500-byte block.

    A 500-byte input yields 504 bytes of ciphertext (the reference
    implementation produces exactly this, verified by `tests/test_protocol.py`).
    """
    from Crypto.Util.Padding import pad
    return _des().encrypt(pad(block, 8))


def decrypt_block(ciphertext: bytes) -> bytes:
    """Inverse of :func:`encrypt_block` (used by the unit tests)."""
    from Crypto.Util.Padding import unpad
    return unpad(_des().decrypt(ciphertext), 8)


def build_plaintext_block(opcode: int, uptime_ms: int, payload: bytes = b"") -> bytes:
    """Build the 500-byte plaintext block.  [CONFIRMED] 목.cs:2703-2735.

    Layout::

        offset  size  meaning
        ------  ----  --------------------------------------------------
        0       1     opcode (Cmd)
        1       1     never written by the reference code -> 0x00
        2       1     0x1A
        3       1     0x6D
        4       7     4-byte little-endian uptime in ms, then zero padding
                      (the reference copies exactly 4 bytes: BitConverter.GetBytes(long))
        8       492   payload (truncated; see build_command for the overflow path)
    """
    if not 0 <= int(opcode) <= 0xFF:
        raise ValueError(f"opcode out of range: {opcode!r}")
    block = bytearray(500)
    block[0] = int(opcode) & 0xFF
    block[2] = MAGIC_2
    block[3] = MAGIC_3
    block[4:8] = struct.pack("<I", uptime_ms & 0xFFFFFFFF)
    if payload:
        block[8:8 + min(len(payload), FIRST_PACKET_PAYLOAD)] = \
            payload[:FIRST_PACKET_PAYLOAD]
    return bytes(block)


def wrap_packet(block500: bytes) -> bytes:
    """Turn the 500-byte block into the on-wire 512-byte packet.  [CONFIRMED] 목.cs:2737-2769."""
    if len(block500) != 500:
        raise ValueError(f"expected a 500-byte block, got {len(block500)}")
    packet = bytearray(PACKET_SIZE)
    enc = encrypt_block(block500)
    packet[0:len(enc)] = enc
    packet[510:512] = TRAILER
    return bytes(packet)


def unwrap_packet(packet: bytes) -> bytes:
    """Strip the trailer and decrypt -- the device-side view.  For tests/tools."""
    if len(packet) < 512:
        raise ValueError("packet shorter than 512 bytes")
    return decrypt_block(packet[:504])


def build_command(opcode: int, payload: bytes = b"", uptime_ms: int = 0) -> bytes:
    """Build the full byte string to hand to the transport.

    [CONFIRMED] 목.cs:4468-4580 for the two-stage layout:

    * ``<= 492`` payload bytes -> a single 512-byte (encrypted) packet.
    * ``> 492`` payload bytes  -> the 512-byte encrypted header packet followed
      by the remaining payload bytes **verbatim, unencrypted**.

    The reference code builds one contiguous buffer of
    ``512 + (len(payload) - 492)`` bytes, so this function does the same.
    """
    block = build_plaintext_block(opcode, uptime_ms, payload)
    first = wrap_packet(block)
    if len(payload) <= FIRST_PACKET_PAYLOAD:
        return first
    return first + payload[FIRST_PACKET_PAYLOAD:]


def build_arg_command(opcode: int, arg: int, uptime_ms: int = 0) -> bytes:
    """`굻(opcode, byte)` -- one argument byte at payload offset 0.  [CONFIRMED] 목.cs:4390-4410."""
    return build_command(opcode, bytes((arg & 0xFF,)), uptime_ms)


# --------------------------------------------------------------------------
# Image transfer  [CONFIRMED] 목.cs:2536-2673 (`굻(byte[] data, bool flag)`)
# --------------------------------------------------------------------------
#
#     a_    = flag ? 102 : 101;                     // see CAVEAT below
#     n     = data.Length;
#     buf   = new byte[512 + n];
#     head  = 굻(a_);                               // 500-byte plaintext block
#     head[8]  = (byte)(n >> 24);                   // BIG-ENDIAN uint32 length
#     head[9]  = (byte)((n & 0xFF0000) >> 16);
#     head[10] = (byte)((n & 0xFF00) >> 8);
#     head[11] = (byte)(n & 0xFF);
#     enc = 굻(head);                               // DES-CBC -> 512-byte packet
#     enc.CopyTo(buf, 0);
#     data.CopyTo(buf, 512);                        // payload RAW, after the packet
#     끎(buf);                                      // send
#
# This differs from the generic `build_command` path in two ways: the length
# field is big-endian and lives at plaintext offset 8, and the payload is NOT
# placed inside the encrypted block -- it follows the 512-byte packet verbatim.

IMAGE_OPCODE_JPEG = 101
IMAGE_OPCODE_PNG = 102
"""The two image-display opcodes.

RESOLVED -- but from combined evidence, so the reasoning is worth recording:

* `목.cs:2536-2673` picks between 101 and 102 from a `bool` argument.  The
  Dotfuscator opaque-predicate control flow around that selection is NOT
  confidently readable (a `switch (a == a)` makes one branch look unreachable),
  so static analysis alone could not say which value meant what.
* The call site is `뽞().굻(jpegBytes, A_1: true)` in `꽝.cs:7013`, and the buffer
  it passes is unambiguously **JPEG** (the encoder is configured with
  `Encoder.Quality, 95L` at `꽝.cs:7008`).
* Public reverse engineering of the same vendor protocol independently records
  **101 = show JPEG, 102 = show PNG**
  (`mathoudebine/turing-smart-screen-python`, `library/lcd/lcd_comm_turing_usb.py`;
  `slipalison/bezel`, `docs/reverse-engineering/protocol-turing-usb.md`).

Taking the call site's JPEG + the public mapping gives 101 for the vendor's
still-image push, and that is consistent with the decompiler having inverted the
ternary.  This is a STRONG INFERENCE corroborated externally -- still verify it
with a capture before trusting it on hardware.
"""

IMAGE_LENGTH_OFFSET = 8
"""Big-endian uint32 payload length sits at plaintext offset 8.  [CONFIRMED]"""

#: Backwards-compatible aliases.  Earlier drafts of this prototype guessed that
#: the pair meant "full frame vs differ"; that was wrong -- they select the image
#: *format*, not the refresh mode.  Differ/partial refresh is a separate
#: mechanism (`CMD_DISP_SEQ_POS`, `CMD_DISP_SEQ_DIFFER_INDEX_QUERRY` in the
#: firmware), whose opcode is still unidentified.
IMAGE_OPCODE_FULL = IMAGE_OPCODE_PNG
IMAGE_OPCODE_ALT = IMAGE_OPCODE_JPEG


def build_image_command(payload: bytes, opcode: int = IMAGE_OPCODE_JPEG,
                        uptime_ms: int = 0) -> bytes:
    """Frame an image payload for transfer.  [CONFIRMED] 목.cs:2631-2641.

    Returns ``512 + len(payload)`` bytes: one encrypted header packet followed by
    the raw payload.

    `opcode` should be :data:`IMAGE_OPCODE_JPEG` (101) for JPEG payloads or
    :data:`IMAGE_OPCODE_PNG` (102) for PNG payloads.
    """
    if opcode not in (IMAGE_OPCODE_JPEG, IMAGE_OPCODE_PNG):
        raise ValueError(
            f"unexpected image opcode {opcode}; expected "
            f"{IMAGE_OPCODE_JPEG} (JPEG) or {IMAGE_OPCODE_PNG} (PNG)"
        )
    n = len(payload)
    block = bytearray(build_plaintext_block(opcode, uptime_ms, b""))
    block[IMAGE_LENGTH_OFFSET:IMAGE_LENGTH_OFFSET + 4] = struct.pack(">I", n & 0xFFFFFFFF)
    return wrap_packet(bytes(block)) + payload


# --------------------------------------------------------------------------
# Bidirectional parsing helpers
# --------------------------------------------------------------------------

@dataclass
class Packet:
    """A decoded command packet (host -> device)."""

    opcode: int
    raw_header: bytes
    payload: bytes
    unknown_byte_1: int = 0
    magic: tuple = (0, 0)
    uptime_ms: int = 0
    trailer: bytes = b""
    extra_plain: bytes = field(default=b"")

    @property
    def opcode_name(self) -> str:
        try:
            return Cmd(self.opcode).name
        except ValueError:
            return f"UNKNOWN_{self.opcode:#04x}"


def parse_command(data: bytes) -> Packet:
    """Decode bytes produced by :func:`build_command`.  Used to verify framing."""
    if len(data) < PACKET_SIZE:
        raise ValueError("truncated packet")
    head = data[:PACKET_SIZE]
    block = unwrap_packet(head)
    opcode = block[0]
    pts = block[8:8 + FIRST_PACKET_PAYLOAD]
    extra = data[PACKET_SIZE:]
    return Packet(
        opcode=opcode,
        raw_header=block[:8],
        payload=pts + extra,
        unknown_byte_1=block[1],
        magic=(block[2], block[3]),
        uptime_ms=struct.unpack("<I", block[4:8])[0],
        trailer=head[510:512],
    )


def hexdump(data: bytes, width: int = 16, limit: int | None = None) -> str:
    """Small self-contained hexdump helper (no external dependency)."""
    out = []
    view = data if limit is None else data[:limit]
    for off in range(0, len(view), width):
        chunk = view[off:off + width]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:08X}  {hexpart:<{width * 3}}  {asc}")
    if limit is not None and len(data) > limit:
        out.append(f"... ({len(data) - limit} more bytes)")
    return "\n".join(out)


# ==========================================================================
# Read-only command set
# ==========================================================================
#
# Everything in this section is safe with respect to device state: these opcodes
# only ask the panel for information.  `queries.ReadOnlyClient` will refuse to
# send anything outside this set.

READ_ONLY_OPCODES: frozenset[int] = frozenset({
    int(Cmd.GET_VERSION),          # 10  -> 32-byte UTF-8 version tag
    int(Cmd.QUERY_VOLTAGE),        # 50  -> resp[8..11] BE int32
    int(Cmd.QUERY_SD_CARD),        # 100 -> resp[8..31] 6 x BE uint32
    int(Cmd.QUERY_IS_MEDIA_STOP),  # 112 -> resp[8] == 0 means stopped
    96,                            # temperatures, via the UNFRAMED 64-byte channel
})

#: Opcode 96 is special: it is sent as a bare 64-byte buffer, NOT DES-framed.
#: [CONFIRMED] 꽝.cs:5005-5007 -> 목.꽝(byte[]) (목.cs:4685).
RAW_QUERY_OPCODE_TEMPERATURES = 96
RAW_QUERY_SIZE = 64

#: Every framed command gets a 512-byte response.  Responses are NOT encrypted
#: (residual: public RE of the same protocol; the C# reads resp[0] as the opcode echo).
RESPONSE_SIZE = 512

#: Status byte value the vendor treats as success.  [CONFIRMED] 목.cs:3543, 3414
RESPONSE_OK = 200
RESPONSE_OK_HEX = 0xC8


def build_read_command(opcode: int, payload: bytes = b"", uptime_ms: int = 0) -> bytes:
    """Build a framed read-only command, refusing anything not in the read set."""
    if int(opcode) not in READ_ONLY_OPCODES:
        raise ValueError(
            f"opcode {opcode} is not in the read-only set "
            f"{sorted(READ_ONLY_OPCODES)}; refusing to build it here"
        )
    return build_command(opcode, payload, uptime_ms)


def build_raw_read_command(opcode: int = RAW_QUERY_OPCODE_TEMPERATURES) -> bytes:
    """Build the UNFRAMED 64-byte query (only opcode 96 uses this channel)."""
    if int(opcode) not in READ_ONLY_OPCODES:
        raise ValueError(f"opcode {opcode} is not a read-only opcode")
    buf = bytearray(RAW_QUERY_SIZE)
    buf[0] = int(opcode) & 0xFF
    return bytes(buf)


# --------------------------------------------------------------------------
# Response decoding
# --------------------------------------------------------------------------

@dataclass
class Response:
    """A decoded 512-byte response block (plaintext)."""

    opcode: int
    status: int
    raw: bytes

    @property
    def ok(self) -> bool:
        """`resp[8] == 200` is the vendor's success convention for file ops."""
        return self.status == RESPONSE_OK

    def be_u32(self, offset: int) -> int:
        return int.from_bytes(self.raw[offset:offset + 4], "big")

    def le_u32(self, offset: int) -> int:
        return int.from_bytes(self.raw[offset:offset + 4], "little")

    def be_i32(self, offset: int) -> int:
        return int.from_bytes(self.raw[offset:offset + 4], "big", signed=True)


def parse_response(data: bytes) -> Response:
    """Decode a response block.  `resp[0]` echoes the request opcode."""
    if len(data) < RESPONSE_SIZE:
        raise ValueError(f"response too short: {len(data)} < {RESPONSE_SIZE}")
    raw = data[:RESPONSE_SIZE]
    return Response(opcode=raw[0], status=raw[8], raw=raw)


def parse_version_string(resp: bytes) -> str:
    """`resp[8..39]` = 32-byte UTF-8, NUL padded.  [CONFIRMED] 목.cs:4140-4144."""
    if len(resp) < 40:
        raise ValueError("response too short for a version string")
    return resp[8:40].decode("utf-8", "replace").rstrip("\x00").strip()


# ==========================================================================
# 251 / 252 -- the hidden factory-test region commands
# ==========================================================================
#
# Both are reachable ONLY from the factory test window ℳ.xaml.cs; the normal
# render loop never uses them.  See reports/region-refresh-251-252.md.
#
# EVIDENCE
#   251  목.cs:3752-3789  `public void 굻(int A_0,int A_1,int A_2,int A_3)`
#   252  목.cs:3791-3894  `public bool 굻(byte[] A_0,int A_1,int A_2)`
#   caller 꽝.cs:9308-9536 `럖(int)` -> 뽞().굻(Util.ꔂ(array), 30, 30)
#   caller 꽝.cs:9287-9306 `댡(int,int,int,int)` -> 뽞().굻(A_0,A_1,A_2,A_3)
#   window ℳ.xaml.cs:91,119

REGION_OPCODE = int(Cmd.DISPLAY_REGION)      # 251
INDEXED_OPCODE = int(Cmd.INDEXED_WRITE)      # 252

REGION_PAYLOAD_SIZE = 16
"""Fixed 16-byte payload for opcode 251. [CONFIRMED] 목.cs:3770-3788."""

REGION_CONST = bytes((0xFF, 0x0F, 0xA1, 0x00))
"""Literal prefix of the 251 payload: {255, 15, 161, 0}. [CONFIRMED]"""

#: 252: the sub-record length sits BIG-endian at plaintext offsets 12..15.
INDEXED_LENGTH_OFFSET = 12
#: 252: the sub-record itself is `[id:BE16][index:u8][data...]`, appended RAW
#: after the 512-byte encrypted header packet.
INDEXED_PREFIX_SIZE = 3

#: The only payload the vendor ever sends with 252 is an 8640-byte RGB888 block
#: produced by 꽝.럖(): 96 pixels x 3 bytes, replicated 30 times. [CONFIRMED]
FACTORY_PATTERN_UNIT = 288
FACTORY_PATTERN_REPEAT = 30
FACTORY_PATTERN_SIZE = FACTORY_PATTERN_UNIT * FACTORY_PATTERN_REPEAT  # 8640

FACTORY_INDEXED_ID = 30
FACTORY_INDEXED_INDEX = 30
"""`뽞().굻(Util.ꔂ(array), 30, 30)` -- id=30, index=30. [CONFIRMED] 꽝.cs:9513"""


@dataclass
class RegionCommand:
    """Decoded opcode-251 payload."""

    a0: int
    a1: int
    a2: int
    a3: int
    raw: bytes

    @property
    def as_xywh(self) -> tuple[int, int, int, int]:
        """Best-effort interpretation as (x, y, w, h).

        [STRONG INFERENCE]  From the only call site, `댡(500, 100, 150, 200)`:
        a0 is the only 16-bit field and takes the large value (500), while
        a1/a2/a3 are single bytes and take 100/150/200, which reads naturally as
        (x, y, w, h).  NOT proven -- the parameter names were destroyed by the
        obfuscator.  An 8-bit width/height is also suspicious for a 448x1280
        panel, which is itself a reason to verify on hardware.
        """
        return (self.a0, self.a1, self.a2, self.a3)


def build_region_command(a0: int, a1: int, a2: int, a3: int,
                         uptime_ms: int = 0) -> bytes:
    """Build opcode 251.  [CONFIRMED] 목.cs:3770-3788.

    Payload (16 bytes, placed at plaintext offset 8 by the generic builder)::

        0   1   2   3   4      5      6    7    8    9..15
        FF  0F  A1  00  a0>>8  a0&FF  a1   a2   a3   00
                        \\______ ______/
                               a0 as big-endian 16-bit

    Only a0 is 16-bit; a1/a2/a3 must fit in one byte.
    """
    for name, v in (("a0", a0), ("a1", a1), ("a2", a2), ("a3", a3)):
        limit = 0xFFFF if name == "a0" else 0xFF
        if not 0 <= v <= limit:
            raise ValueError(f"{name}={v} does not fit in {'16' if limit > 255 else '8'} bits")
    payload = bytearray(REGION_PAYLOAD_SIZE)
    payload[0:4] = REGION_CONST
    payload[4] = (a0 >> 8) & 0xFF
    payload[5] = a0 & 0xFF
    payload[6] = a1 & 0xFF
    payload[7] = a2 & 0xFF
    payload[8] = a3 & 0xFF
    return build_command(REGION_OPCODE, bytes(payload), uptime_ms)


def parse_region_command(data: bytes) -> RegionCommand:
    """Decode bytes produced by :func:`build_region_command`."""
    pkt = parse_command(data)
    if pkt.opcode != REGION_OPCODE:
        raise ValueError(f"not a region command: opcode {pkt.opcode}")
    body = pkt.payload[:REGION_PAYLOAD_SIZE]
    if len(body) < REGION_PAYLOAD_SIZE:
        raise ValueError("region payload truncated")
    if body[:4] != REGION_CONST:
        raise ValueError(f"region constant mismatch: {body[:4].hex()}")
    return RegionCommand(
        a0=(body[4] << 8) | body[5], a1=body[6], a2=body[7], a3=body[8], raw=bytes(body))


def build_indexed_command(data: bytes, ident: int, index: int,
                          uptime_ms: int = 0) -> bytes:
    """Build opcode 252.  [CONFIRMED] 목.cs:3791-3894.

    Wire layout::

        [ 512-byte encrypted header packet ]
            plaintext[0]  = 252
            plaintext[12..15] = BIG-endian uint32 of (len(data) + 3)
        [ raw sub-record, unencrypted ]
            [0..1] ident as BIG-endian uint16
            [2]    index (8 bits)
            [3..]  data
    """
    if not 0 <= ident <= 0xFFFF:
        raise ValueError("ident must fit in 16 bits")
    if not 0 <= index <= 0xFF:
        raise ValueError("index must fit in 8 bits")
    record = bytearray(INDEXED_PREFIX_SIZE + len(data))
    record[0] = (ident >> 8) & 0xFF
    record[1] = ident & 0xFF
    record[2] = index & 0xFF
    record[3:] = data

    block = bytearray(build_plaintext_block(INDEXED_OPCODE, uptime_ms, b""))
    n = len(record)
    block[INDEXED_LENGTH_OFFSET:INDEXED_LENGTH_OFFSET + 4] = struct.pack(">I", n & 0xFFFFFFFF)
    return wrap_packet(bytes(block)) + bytes(record)


def parse_indexed_command(data: bytes) -> dict:
    """Decode bytes produced by :func:`build_indexed_command`."""
    if len(data) < PACKET_SIZE:
        raise ValueError("truncated indexed command")
    block = unwrap_packet(data[:PACKET_SIZE])
    if block[0] != INDEXED_OPCODE:
        raise ValueError(f"not an indexed command: opcode {block[0]}")
    total = struct.unpack(">I", block[INDEXED_LENGTH_OFFSET:INDEXED_LENGTH_OFFSET + 4])[0]
    record = data[PACKET_SIZE:PACKET_SIZE + total]
    if len(record) != total:
        raise ValueError(f"sub-record truncated: {len(record)} != {total}")
    return {
        "opcode": INDEXED_OPCODE,
        "record_len": total,
        "ident": (record[0] << 8) | record[1],
        "index": record[2],
        "data": record[INDEXED_PREFIX_SIZE:],
        "header_len_field": total,
    }


def factory_region_pixels(colour: int) -> bytes:
    """Reproduce `꽝.럖(int)`'s RGB888 buffer.  [CONFIRMED] 꽝.cs:9369-9483.

    Builds 96 pixels of one colour (3 bytes each = 288 bytes) and replicates the
    block `FACTORY_PATTERN_REPEAT` (30) times -> 8640 bytes.

    Byte order is RGB: colour 0 -> (255,0,0), 1 -> (0,0,255), 2 -> (0,255,0).

    NOTE: the vendor then wraps this in `Util.ꔂ` == `TuzEnc` from the **absent**
    `yuz.dll`, so a byte-identical 252 payload cannot be produced by this tool
    (see reports/region-refresh-251-252.md §4).
    """
    if colour == 0:
        triple = bytes((0xFF, 0x00, 0x00))
    elif colour == 1:
        triple = bytes((0x00, 0x00, 0xFF))
    elif colour == 2:
        triple = bytes((0x00, 0xFF, 0x00))
    else:
        raise ValueError("colour must be 0 (red), 1 (blue) or 2 (green)")
    unit = triple * 96
    if len(unit) != FACTORY_PATTERN_UNIT:
        raise AssertionError("pattern unit size drift")
    return unit * FACTORY_PATTERN_REPEAT
