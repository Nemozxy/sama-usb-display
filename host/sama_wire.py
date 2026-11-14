"""Wire-level helpers shared by the night probes.  No device, no serial, no PIL.

Keeping these here rather than in a probe script is deliberate: the rehearsal test
must be able to import them on a host without the device stack installed, so a
quoting or attribution mistake is caught before a frame is spent.
"""
from __future__ import annotations

VENDOR_BLOCK = 1024
VENDOR_WORDS = (b'play_img_ok', b'play_img_error', b'play_img_err_noexec')


def strip_vendor_status(raw: bytes) -> tuple[bytes, str]:
    """Drop exactly one complete factory status block if the reply starts with it.

    The block is a fixed 1024 bytes -- `play_img_ok` or `play_img_error` followed by
    NUL padding -- and it carries no trailing newline, so the first shell line starts
    immediately after the padding. Anything else is returned untouched.
    """
    for word in VENDOR_WORDS:
        if not raw.startswith(word):
            continue
        if len(raw) < VENDOR_BLOCK:
            return raw, 'SHORT_VENDOR_BLOCK'
        block = raw[:VENDOR_BLOCK]
        if block[len(word):].strip(b'\x00'):
            return raw, 'MALFORMED_VENDOR_BLOCK'
        return raw[VENDOR_BLOCK:], 'VENDOR_BLOCK_STRIPPED'
    return raw, 'NO_VENDOR_BLOCK'


def bracket(body: bytes, token: str) -> tuple[str, list[str], list[str]]:
    """Return (state, tagged, bare) for a `<TOK>B ... <TOK>E` block."""
    if not body:
        return 'EMPTY', [], []
    text = body.decode('latin-1', 'replace')
    padded = text if text.startswith('\n') else '\n' + text
    if not padded.endswith('\n'):
        padded += '\n'
    import re
    match = re.search(r'\n%sB\n(.*?)\n%sE' % (re.escape(token), re.escape(token)), padded, re.S)
    if not match:
        return 'NO_BRACKET', [], [line.strip() for line in padded.splitlines() if line.strip()][:12]
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    tagged = [line for line in lines if line.startswith(token + ':')]
    bare = [line for line in lines if not line.startswith(token + ':')]
    return ('OK' if tagged else ('BARE_LINES' if bare else 'EMPTY_BODY')), tagged, bare


class CheckedPort:
    """Delegating serial port that refuses to write a frame that lost its payload."""

    def __init__(self, port, payload: bytes):
        self._port = port
        self._payload = payload

    def write(self, frame: bytes) -> int:
        problems = []
        if len(frame) != 250:
            problems.append('frame length %d' % len(frame))
        if frame[0] != 140 or frame[1:3] != b'\xef\x69':
            problems.append('header %s' % frame[:3].hex())
        value = int.from_bytes(frame[3:7], 'big')
        if value != len(self._payload):
            problems.append('declared %d != payload %d' % (value, len(self._payload)))
        if value == 0:
            problems.append('empty payload')
        if frame[10:10 + value] != self._payload:
            problems.append('payload bytes differ')
        if any(frame[10 + value:]):
            problems.append('trailing bytes after payload')
        if problems:
            raise SystemExit('refusing to transmit: ' + '; '.join(problems))
        return self._port.write(frame)

    def __getattr__(self, name):
        return getattr(self._port, name)
