"""Device side of the one-click boot-video flow.  Imports the verified pieces.

Every persistent write here goes to exactly one of two paths, both approved for
this class of work: /mnt/UDISK/video/ and /app.cfg. Nothing touches the boot
chain, the factory service, or raw flash.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sama_wire import CheckedPort, bracket, strip_vendor_status

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'onboard'
VIDEO_DIR = '/mnt/UDISK/video'
IMG_DIR = '/mnt/UDISK/img'
CFG = '/app.cfg'
CFG_BACKUP = '/mnt/UDISK/app.cfg.orig'
TMP_TARGET = '/tmp/suv'
CAP = 200
ORIGINAL_CFG_MD5 = '5e9493e4c60eebe0db7a815fcdc98f14'
TEMPLATE = ROOT / 'host' / 'assets' / 'onboard-app.cfg'
MAX_MEDIA_BYTES = 64 * 1024 * 1024

PERSISTENT_PREFIXES = ('/mnt/UDISK/',)
PERSISTENT_FILES = ('/app.cfg',)


class ChannelSilent(RuntimeError):
    """The panel's command receiver answered nothing; only a re-plug recovers it."""


def _settle(port, seconds: float) -> int:
    """Read and discard for a fixed window, regardless of quiet.

    `_drain_until_quiet` returns immediately when nothing has arrived yet, which is
    exactly the case where a late vendor block is about to land. This waits for the
    whole window so those bytes are consumed here instead of blocking the device.
    """
    import time
    deadline = time.monotonic() + seconds
    drained = 0
    while time.monotonic() < deadline:
        try:
            chunk = port.read(1024)
        except Exception:                                        # noqa: BLE001
            break
        if chunk:
            drained += len(chunk)
    return drained


def _lines(result) -> list[str]:
    return list(result.get('bare') or []) + list(result.get('tagged') or [])


SETTLE_SECONDS = 6.0
DONE_MARKER = bytes.fromhex('66696c655f7265765f646f6e6500696d675f73686f775f69')
BULK_DONE_TIMEOUT = 15.0


class Session:
    """One open serial port reused for a whole multi-frame operation.

    Opening and closing per frame worked while nothing was playing, but with a
    short video looping the factory keeps writing status blocks; closing the port
    between frames leaves a block in flight and the gadget stalls until a re-plug.
    Holding one port open and draining for longer than the loop period avoids that
    hand-off entirely.
    """

    def __init__(self, port, port_name: str):
        self.port = port
        self.port_name = port_name
        self.frames = 0

    def close(self, settle_seconds: float = SETTLE_SECONDS) -> int:
        drained = _settle(self.port, settle_seconds)
        try:
            self.port.close()
        except Exception:                                        # noqa: BLE001
            pass
        return drained


def open_session(port_name=None):
    """Open the selected image port once; module import remains device-free."""
    import serial
    import serial_proto
    from sama_screen import find_panel_port

    if port_name is None:
        port_name, _ = find_panel_port()
    if not port_name:
        raise ChannelSilent('panel port not found: check the cable and replug')
    port = serial.Serial(port_name, serial_proto.SERIAL_BAUD, timeout=0.2, write_timeout=5.0)
    return Session(port, port_name)


_SESSION = None


def session_scope(port_name=None):
    """Hold one selected port for the whole bounded operation."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        global _SESSION
        if _SESSION is not None:
            raise RuntimeError('an onboard session is already active')
        from session_lock import controller_lock
        with controller_lock(DATA):
            _SESSION = open_session(port_name)
            try:
                yield _SESSION
            finally:
                _SESSION.close()
                _SESSION = None

    return scope()


def _contains(result, needle: str) -> bool:
    """True when any reported line contains the value.

    `md5sum` prints "<hash>  -" and `wc -c` prints the bare number, so an exact
    element comparison would reject a perfectly good reply.
    """
    return any(needle in line for line in _lines(result))


def wrap(body: str, token: str) -> str:
    return "/dev/null;{ echo %sB;%s;echo %sE;} >/dev/ttyGS0 2>&1;#.png" % (token, body, token)


def _validated_redirects(command: str, allow_write=()):
    import re
    assignments = dict(re.findall(r'(?:^|;)\s*([A-Za-z_]\w*)=([^\s;&|)]+)', command))
    redirects = []
    for path in re.findall(r'>>?\s*([^\s;&|)]+)', command):
        variable = re.fullmatch(r'\$([A-Za-z_]\w*)', path)
        if variable:
            if variable.group(1) not in assignments:
                raise RuntimeError('unbound redirect variable: %s' % path)
            path = assignments[variable.group(1)]
        redirects.append(path)
    allowed = {'/dev/ttyGS0', '/dev/null'} | set(allow_write)
    if set(redirects) - allowed or '/dev/ttyGS0' not in redirects:
        raise RuntimeError('redirects outside the allowlist: %s' % redirects)
    return redirects


def shell(body: str, token: str, grant: str, purpose: str, allow_write=(), session=None) -> dict:
    """Send one bounded command, without retrying or opening an implicit session."""
    import serial
    import serial_proto
    from sama_screen import find_panel_port

    command = wrap(body, token)
    payload = command.encode('ascii')
    redirects = _validated_redirects(command, allow_write)
    if len(payload) > CAP:
        raise RuntimeError('payload %d over cap %d' % (len(payload), CAP))
    for path in allow_write:
        if path not in purpose:
            raise RuntimeError('%s is not named in the purpose' % path)
        if not (path.startswith('/tmp/') or path.startswith(PERSISTENT_PREFIXES)
                or path in PERSISTENT_FILES):
            raise RuntimeError('path not writable: %s' % path)

    global _SESSION
    if session is None:
        session = _SESSION
    owned = session is None
    if owned:
        port_name, _ = find_panel_port()
        if not port_name:
            raise ChannelSilent('panel port not found; check the USB connection')
        port = serial.Serial(port_name, serial_proto.SERIAL_BAUD, timeout=0.2, write_timeout=5.0)
    else:
        port = session.port
    raw = bytearray()
    try:
        port.reset_input_buffer()
        frame = serial_proto.build_frame(140, len(payload), payload, 0)
        try:
            written = CheckedPort(port, payload).write(frame)
            if written != len(frame):
                raise OSError('short shell frame write: %s/%s' % (written, len(frame)))
            port.flush()
        except Exception as exc:
            raise OSError('shell write failed: operation=%s token=%s: %s'
                          % (grant, token, exc)) from exc
        started = time.monotonic()
        while time.monotonic() - started < 60:
            chunk = port.read(1024)
            if chunk:
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise RuntimeError('shell reply exceeded 64 KiB')
                if (token + 'E').encode('ascii') in raw:
                    break
            elif not raw and time.monotonic() - started >= 30:
                break
        _settle(port, SETTLE_SECONDS)
        if session is not None:
            session.frames += 1
    finally:
        if owned:
            try:
                port.close()
            except Exception:
                pass
    if not raw:
        raise ChannelSilent('no reply at all: unplug and replug the panel USB, then retry')
    if (token + 'E').encode('ascii') not in raw:
        raise RuntimeError('incomplete shell reply; not retrying automatically')
    body_bytes, note = strip_vendor_status(bytes(raw))
    state, tagged, bare = bracket(body_bytes, token)
    fields = {}
    for line in tagged:
        text = line[len(token) + 1:]
        key, sep, value = text.partition('=')
        if sep:
            fields[key.strip().lower()] = value.strip()
        else:
            fields[text.strip().lower()] = 'present'
    return dict(state=state, fields=fields, tagged=tagged, bare=bare, note=note,
                rx_bytes=len(raw))


def build_config(video_path: str, mode: int = 2) -> bytes:
    """132 bytes: original template, mode byte 1 set to 2, path at offset 5."""
    template = TEMPLATE.read_bytes()
    if hashlib.md5(template).hexdigest() != ORIGINAL_CFG_MD5:
        raise RuntimeError('config template changed: %s' % hashlib.md5(template).hexdigest())
    encoded = video_path.encode('ascii')
    if len(encoded) > 126:
        raise RuntimeError('device path too long for the 132-byte config')
    data = bytearray(template)
    data[1] = mode
    data[5:] = b'\x00' * (len(data) - 5)
    data[5:5 + len(encoded)] = encoded
    return bytes(data)


def upload_bytes(data: bytes, target: str, grant: str, purpose: str) -> dict:
    result = _bulk_send(data, target, grant)
    return dict(result, target=target, md5=hashlib.md5(data).hexdigest())


def stop_video() -> dict:
    """Request the factory stop-video command before a media upload."""
    import serial_proto
    if _SESSION is None:
        raise RuntimeError('stop_video needs an open onboard session')
    frame = serial_proto.build_frame(121, 1)
    port = _SESSION.port
    try:
        written = port.write(frame)
        if written != len(frame):
            raise OSError('short stop-video write: %s/%s' % (written, len(frame)))
        port.flush()
    except Exception as exc:
        raise OSError('stop-video write failed before upload: %s' % exc) from exc
    _settle(port, 2.0)
    _SESSION.frames += 1
    return dict(requested=True, playback_verified=False)


def _bulk_send(data: bytes, target: str, label: str, batch_frames: int = 1024,
              already_created: bool = False, progress=None) -> dict:
    """Push one bounded file over cmd111 in the current exclusive session."""
    import struct
    import time as _time
    from serial_proto import build_frame

    if not 1 <= len(data) <= MAX_MEDIA_BYTES:
        raise ValueError('media size must be 1 byte to 64 MiB')
    if not (target.startswith('/tmp/') or target.startswith(PERSISTENT_PREFIXES)):
        raise ValueError('bulk target is outside the media allowlist')
    if not 1 <= batch_frames <= 1024:
        raise ValueError('batch_frames must be 1..1024')
    if not already_created:
        created = shell('rm -f %s;(set -C;:>%s) 2>/dev/null&&echo OK:OWN' % (target, target), 'OK',
                        label + '-create',
                        'remove and exclusively recreate %s for the bulk channel' % target,
                        allow_write=(target,))
        if created['fields'].get('own') != 'present':
            raise RuntimeError('could not create %s exclusively' % target)

    begin = build_frame(111, len(target), target.encode('ascii') + struct.pack('<I', len(data)), 0)
    frames = [begin] + [data[offset:offset + 249].ljust(250, b'\x00')
                        for offset in range(0, len(data), 249)]
    port = (_SESSION.port if _SESSION is not None else None)
    if port is None:
        raise RuntimeError('bulk send needs an open session')

    try:
        if port.write(begin) != len(begin):
            raise OSError('short cmd111 begin write')
        port.flush()
    except Exception as exc:
        raise OSError('cmd111 begin write failed: target=%s bytes=%d: %s'
                      % (target, len(data), exc)) from exc
    begin_reply = b''
    begin_deadline = _time.monotonic() + 5
    while _time.monotonic() < begin_deadline:
        begin_reply += port.read(2048)
        if begin_reply.startswith(b'create_success'):
            break
    if not begin_reply.startswith(b'create_success'):
        raise RuntimeError('cmd111 begin was not accepted: %r' % begin_reply[:40])

    batches = 0
    status_bytes = 0
    if progress is not None:
        progress(0.0)
    for index in range(1, len(frames), batch_frames):
        group = frames[index:index + batch_frames]
        payload = b''.join(group)
        try:
            count = port.write(payload)
            if count != len(payload):
                raise OSError('short write: %s/%s' % (count, len(payload)))
            port.flush()
        except Exception as exc:
            raise OSError('cmd111 data write failed: batch=%d frame_offset=%d '
                          'data_offset=%d batch_bytes=%d total_bytes=%d: %s'
                          % (batches, index - 1, (index - 1) * 249,
                             len(payload), len(data), exc)) from exc
        batches += 1
        while getattr(port, 'in_waiting', 0):
            chunk = port.read(min(4096, port.in_waiting))
            if not chunk:
                break
            status_bytes += len(chunk)
            if status_bytes > 65536:
                raise RuntimeError('cmd111 status bytes exceeded 64 KiB during upload')
        if progress is not None:
            progress(min(0.99, (index - 1 + len(group)) * 249 / len(data)))
    done = b''
    deadline = _time.monotonic() + BULK_DONE_TIMEOUT
    while _time.monotonic() < deadline:
        chunk = port.read(4096)
        if chunk:
            done += chunk
            if DONE_MARKER in done:
                break
    if DONE_MARKER not in done:
        raise RuntimeError('cmd111 did not report completion: %r' % done[:40])
    if progress is not None:
        progress(1.0)
    if _SESSION is not None:
        _SESSION.frames += len(frames)
    return dict(frames=len(frames), batches=batches, bytes=len(data), status_bytes=status_bytes,
                first_reply=begin_reply[:24].decode('latin-1', 'replace'),
                done_reply=done[:24].decode('latin-1', 'replace'))


def _fits_shell(body: str, token: str) -> bool:
    return len(wrap(body, token).encode('ascii')) <= CAP


def _measurement_pair(lines, size: int, md5: str) -> bool:
    return (len(lines) == 2 and lines[0].strip() == str(size)
            and lines[1].split()[0] == md5)


VIDEO_NEXT = '/mnt/UDISK/video/.sama-next'


def _synced(result):
    return result['fields'].get('rc') == '0' and result['fields'].get('sync') == '0'


def _stage_device_file(source: str, size: int, md5: str, grant: str):
    staged = shell('t=%s;cat %s>$t;r=$?;sync;s=$?;echo CP:RC=$r;echo CP:SYNC=$s;'
                   'wc -c<$t;md5sum<$t' % (VIDEO_NEXT, source),
                   'CP', grant,
                   'copy %s to %s, sync it and verify before replacing active media'
                   % (source, VIDEO_NEXT), allow_write=(VIDEO_NEXT,))
    lines = [line.strip() for line in staged['bare'] if line.strip()]
    ok = _synced(staged) and _measurement_pair(lines, size, md5)
    return staged, ok


def _commit_staged_file(device_path: str, size: int, md5: str, grant: str,
                        staging: str = VIDEO_NEXT):
    committed = shell('t=%s;d=%s;mv -f $t $d;r=$?;sync;s=$?;echo CM:RC=$r;'
                      'echo CM:SYNC=$s;wc -c<$d;md5sum<$d' % (staging, device_path),
                      'CM', grant,
                      'atomically replace %s with synced %s, sync and verify'
                      % (device_path, staging),
                      allow_write=(staging, device_path))
    lines = [line.strip() for line in committed['bare'] if line.strip()]
    ok = _synced(committed) and _measurement_pair(lines, size, md5)
    return committed, ok


def push_video(local: Path, device_name: str = 'sama-own.mp4', progress=None, step=None) -> dict:
    """Stage, sync and atomically replace a video without truncating the active file."""
    if _SESSION is None:
        with session_scope():
            return push_video(local, device_name, progress, step)

    def notify(stage):
        if step is not None:
            step(stage)

    data = local.read_bytes()
    device_path = '%s/%s' % (VIDEO_DIR, device_name)
    wants = hashlib.md5(data).hexdigest()
    notify('backup')
    stop_video()
    kept = shell('cp {0} {1};r=$?;sync;s=$?;echo KP:RC=$r;echo KP:SYNC=$s'
                 .format(device_path, VIDEO_BACKUP),
                 'KP', 'flow-keep-good',
                 'keep and sync a copy of the current boot video at %s' % VIDEO_BACKUP,
                 allow_write=(VIDEO_BACKUP,))
    notify('upload')
    result = _bulk_send(data, TMP_TARGET, 'flow-video', progress=progress)
    notify('verify')
    verify = shell('wc -c<%s;md5sum<%s' % (TMP_TARGET, TMP_TARGET), 'VR', 'flow-verify-suv',
                   'verify the bulk transfer landed intact')
    lines = [line.strip() for line in verify['bare'] if line.strip()]
    size_ok = _measurement_pair(lines, len(data), wants)
    base = dict(device_path=device_path, size=len(data), md5=wants,
                kept_previous=_synced(kept), verified_on_tmp=size_ok,
                combined_shell=False, raw_bulk=result)
    if not size_ok:
        return dict(base, staged_and_verified=False, copied_and_verified=False)
    notify('stage')
    staged, staged_ok = _stage_device_file(TMP_TARGET, len(data), wants, 'flow-stage-video')
    if not staged_ok:
        return dict(base, staged_and_verified=False, copied_and_verified=False)
    notify('commit')
    committed, copied = _commit_staged_file(device_path, len(data), wants, 'flow-commit-video')
    return dict(base, staged_and_verified=staged_ok, copied_and_verified=copied)


def rollback_video(device_name: str = 'sama-own.mp4') -> dict:
    """Restore the fixed previous-version slot through a synced atomic replacement."""
    device_path = '%s/%s' % (VIDEO_DIR, device_name)
    previous = shell('if [ -e {0} ];then wc -c<{0};md5sum<{0};else echo NO_PREVIOUS;fi'
                     .format(VIDEO_BACKUP), 'RV', 'flow-rollback-read',
                     'read the size and digest of previous video %s' % VIDEO_BACKUP)
    if 'NO_PREVIOUS' in _lines(previous):
        return dict(rolled_back=False, reason='no saved previous video')
    lines = [line.strip() for line in previous['bare'] if line.strip()]
    if len(lines) != 2 or not lines[0].isdigit() or len(lines[1].split()[0]) != 32:
        raise RuntimeError('could not verify the previous video before rollback')
    size = int(lines[0])
    wants = lines[1].split()[0]
    staged, staged_ok = _stage_device_file(VIDEO_BACKUP, size, wants, 'flow-rollback-stage')
    if not staged_ok:
        return dict(rolled_back=False, reason='previous-video staging or sync failed')
    committed, ok = _commit_staged_file(device_path, size, wants, 'flow-rollback-commit')
    return dict(rolled_back=ok, device_path=device_path, size=size, device_md5=wants)


def snapshot_config() -> dict:
    """One frame: keep the current /app.cfg so mode 2 can be restored in one frame.

    Cheaper and more faithful than rebuilding it: whatever is live now (path
    included) is exactly what `set-mode 2` should put back.
    """
    result = shell('if [ ! -e %s ];then cp %s %s;fi;echo SN:RC=$?;md5sum<%s'
                   % (VIDEO_CFG_COPY, CFG, VIDEO_CFG_COPY, VIDEO_CFG_COPY),
                   'SN', 'flow-config-snapshot',
                   'keep the live %s as %s so the video mode can be restored in one frame'
                   % (CFG, VIDEO_CFG_COPY), allow_write=(VIDEO_CFG_COPY,))
    return dict(state=result['state'], bare=result['bare'])


def apply_config(video_path: str, mode: int = 2) -> dict:
    data = build_config(video_path, mode)
    md5 = hashlib.md5(data).hexdigest()
    tmp = '/tmp/c'
    # The 132 bytes travel over cmd111 rather than the octal channel: one create,
    # one write, one copy, instead of eight shell frames.
    created = shell('rm -f %s;(set -C;:>%s) 2>/dev/null&&echo CC:OWN' % (tmp, tmp), 'CC',
                    'flow-cfg-create', 'reserve %s for the 132-byte config' % tmp,
                    allow_write=(tmp,))
    if created['fields'].get('own') != 'present':
        raise RuntimeError('could not reserve %s' % tmp)
    _bulk_send(data, tmp, 'flow-cfg', already_created=True)
    result = shell('cat %s>%s;r=$?;sync;s=$?;echo WR:RC=$r;echo WR:SYNC=$s;'
                   'wc -c<%s;md5sum<%s' % (tmp, CFG, CFG, CFG),
                   'WR', 'flow-cfg-write',
                   'write and sync %s from %s so the panel boots into mode %d' % (CFG, tmp, mode),
                   allow_write=(CFG,))
    lines = [line.strip() for line in result['bare'] if line.strip()]
    ok = _synced(result) and _measurement_pair(lines, len(data), md5)
    return dict(config_md5=md5, written=ok, mode=mode, path=video_path)


VIDEO_CFG_COPY = '/mnt/UDISK/cv'
VIDEO_BACKUP = '/mnt/UDISK/vprev'


def save_video_config(video_path: str) -> dict:
    """Keep the prepared mode-2 config on the persistent volume.

    Switching modes then costs one frame instead of re-uploading 132 bytes: the
    video path is ignored in picture mode, so only byte 1 has to change.
    """
    data = build_config(video_path, mode=2)
    md5 = hashlib.md5(data).hexdigest()
    upload_bytes(data, '/tmp/appcfg.keep', 'flow-cfgkeep-upload',
                 'upload the mode-2 config to /tmp/appcfg.keep before storing it')
    result = shell('cat /tmp/appcfg.keep>%s;echo SV:RC=$?;md5sum<%s' % (VIDEO_CFG_COPY, VIDEO_CFG_COPY),
                   'SV', 'flow-cfgkeep-store',
                   'store the mode-2 config at %s' % VIDEO_CFG_COPY,
                   allow_write=(VIDEO_CFG_COPY,))
    return dict(stored=_contains(result, md5), md5=md5, path=VIDEO_CFG_COPY)


def set_mode(mode: int) -> dict:
    """Change only byte 1 of /app.cfg. One frame.

    Safe for 0 and 1 because the factory reads the mode byte at boot and picture
    mode ignores the path field. Mode 2 is deliberately NOT done this way: if the
    config currently holds the factory's placeholder path, switching to 2 would
    point the player at nothing. Going back to video uses the stored config.
    """
    if mode not in (0, 1, 2):
        raise ValueError('mode must be 0, 1 or 2')
    if mode == 2:
        raised = restore_video_config()
        return dict(mode=2, via='stored_config', **raised)
    octal = '\\%03o' % mode
    # Read the byte back and compare its digest: a dd that silently wrote nothing
    # would otherwise look exactly like success, and the next boot would run the
    # old mode.
    want = hashlib.md5(bytes([mode])).hexdigest()
    result = shell("printf '%s'|dd of=%s bs=1 seek=1 conv=notrunc;"
                   "echo B=$(dd if=%s bs=1 skip=1 count=1|md5sum)"
                   % (octal, CFG, CFG), 'SM', 'flow-set-mode',
                   'write mode %d into byte 1 of %s and read it back' % (mode, CFG),
                   allow_write=(CFG,))
    values = {}
    for line in _lines(result):
        key, sep, value = line.partition('=')
        if sep and len(key) <= 2:
            values[key.strip().lower()] = value.strip()
    read_back = (values.get('b') or '').split()[0] if values.get('b') else None
    return dict(mode=mode, state=result['state'], confirmed=read_back == want,
                byte_md5=read_back, expected_byte_md5=want,
                config_md5=[line.split()[0] for line in _lines(result) if len(line) >= 32])


def show_image(path: str) -> dict:
    """Display a file on the panel right now, without a power cycle.

    This is the same call the factory makes at boot (`fbviewer <path> 0`), so it
    shows the picture immediately. Video has no callable player — the factory
    decodes in-process — so use reboot_device() for that case instead.
    """
    result = shell('fbviewer %s 0' % path, 'SH', 'flow-show-image',
                   'display %s on the panel now' % path)
    return dict(state=result['state'], shown=result['state'] == 'OK', path=path,
                bare=result['bare'])


def reboot_device(delay_seconds: int = 1) -> dict:
    """Ask the device to restart itself so a new video starts playing.

    Backgrounded with a short delay so the command's own reply still reaches us.
    The device re-enumerates; the caller should expect a few seconds of silence.
    """
    body = 'sync;(sleep %d;/sbin/reboot) &' % delay_seconds
    result = shell(body, 'RE', 'flow-reboot',
                   'restart the panel so the newly configured video starts playing')
    return dict(state=result['state'], requested=True, bare=result['bare'])


def restore_video_config() -> dict:
    """One frame: copy the stored mode-2 config back over /app.cfg."""
    result = shell('if [ -e %s ];then cat %s>%s;echo RV:RC=$?;md5sum<%s;else echo NO_STORED;fi'
                   % (VIDEO_CFG_COPY, VIDEO_CFG_COPY, CFG, CFG),
                   'RV', 'flow-restore-video',
                   'restore %s from %s' % (CFG, VIDEO_CFG_COPY), allow_write=(CFG,))
    if 'NO_STORED' in _lines(result):
        return dict(restored=False, reason='no stored video config; run push again')
    return dict(restored=True, bare=result['bare'])


STATE_FILE = DATA / 'state.json'


def _recall(key: str, max_age_seconds: float = 7 * 24 * 3600):
    """Remember facts that cost a frame to learn, such as where fbviewer lives.

    The channel only survives a couple of frames, so not asking the same question
    twice is worth more than freshness.
    """
    import json as _json
    try:
        data = _json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:                                            # noqa: BLE001
        return None
    entry = data.get(key)
    if not entry or time.time() - entry.get('at', 0) > max_age_seconds:
        return None
    return entry.get('value')


def _remember(key: str, value) -> None:
    import json as _json
    try:
        data = _json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:                                            # noqa: BLE001
        data = {}
    data[key] = {'value': value, 'at': time.time()}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def push_image(local: Path, device_name: str = 's.png', progress=None) -> dict:
    """Put one still image in the factory's picture directory (mode 1)."""
    if _SESSION is None:            # library callers get a session for free
        with session_scope():
            return push_image(local, device_name)
    data = local.read_bytes()
    target = '%s/%s' % (IMG_DIR, device_name)
    stop_video()

    # Picture mode does not decode in-process: the factory runs
    # `fbviewer <dir>/<name> 0` for each entry and ignores failures, so a missing
    # fbviewer would leave the panel looping over nothing with no visible error.
    # The answer is cached, because learning it costs a frame we may not have.
    known = _recall('fbviewer')
    if known:
        viewer = known
    else:
        probe = shell('command -v fbviewer||echo NO_FBVIEWER', 'FB', 'flow-check-fbviewer',
                      'check that the factory picture viewer exists before setting mode 1')
        viewer = (probe['bare'] or [None])[0]
        if 'NO_FBVIEWER' in _lines(probe) or not viewer:
            return dict(ok=False,
                        reason='fbviewer not found on the device; picture mode cannot display anything',
                        probe=probe['bare'])
        _remember('fbviewer', viewer)

    staging = '%s/.sama-next' % IMG_DIR
    prepared = shell('mkdir -p %s;[ -e %s ]||cp %s %s;rm -f %s;(set -C;:>%s);echo PF:OK'
                     % (IMG_DIR, VIDEO_CFG_COPY, CFG, VIDEO_CFG_COPY, staging, staging),
                     'PF', 'flow-prepare-image',
                     'create %s, keep %s as %s, reserve staging file %s'
                     % (IMG_DIR, CFG, VIDEO_CFG_COPY, staging),
                     allow_write=(staging, VIDEO_CFG_COPY))
    sent = _bulk_send(data, staging, 'flow-img', already_created=True, progress=progress)
    wants = hashlib.md5(data).hexdigest()
    checked = shell('wc -c<%s;md5sum<%s' % (staging, staging), 'CK', 'flow-check-image',
                    'verify the staged image written to %s' % staging)
    lines = [line.strip() for line in checked['bare'] if line.strip()]
    staged_ok = _measurement_pair(lines, len(data), wants)
    if not staged_ok:
        return dict(device_path=target, size=len(data), md5=wants, ok=False,
                    prepared=prepared['state'], bulk=sent, copied_and_verified=False)
    committed, copied = _commit_staged_file(target, len(data), wants,
                                           'flow-commit-image', staging=staging)
    return dict(device_path=target, size=len(data), md5=wants, ok=copied,
                prepared=prepared['state'], bulk=sent, copied_and_verified=copied)


def restore() -> dict:
    result = shell('cat %s>%s;echo RS:RC=$?;md5sum<%s' % (CFG_BACKUP, CFG, CFG),
                   'RS', 'flow-restore', 'restore %s from %s' % (CFG, CFG_BACKUP),
                   allow_write=(CFG,))
    ok = _contains(result, ORIGINAL_CFG_MD5)
    return dict(restored=ok, expected=ORIGINAL_CFG_MD5)


def verify() -> dict:
    """Two read-only frames: does the configured media still exist and match?

    Answers the question a user actually asks after a push or a power cycle
    without touching anything: what does the config point at, and is that file
    still there with the size and digest it had.
    """
    values = {}

    def absorb(result):
        for line in _lines(result):
            key, sep, value = line.partition('=')
            if sep and len(key) <= 2:
                values[key.strip().lower()] = value.strip()

    head = shell("echo C=$(md5sum<%s);echo P=$(dd if=%s bs=1 skip=5 2>/dev/null|tr -d '\\000')"
                 % (CFG, CFG), 'VF', 'flow-verify-config',
                 'read the config digest and the media path it points at')
    absorb(head)
    path = values.get('p') or ''
    report = dict(config_md5=(values.get('c') or '').split()[0] if values.get('c') else None,
                  media_path=path, mode=None)
    if not path:
        report.update(reachable=False, reason='no media path in the config')
        return report
    if path.rstrip('/') == IMG_DIR:
        report.update(mode=1, reason='picture mode points at a directory, not a file')
        return report

    probe = shell('echo S=$(wc -c<%s);echo M=$(md5sum<%s)' % (path, path), 'VU',
                  'flow-verify-media',
                  'check that the configured media file still exists with its size and digest')
    absorb(probe)
    report.update(mode=2, size=(values.get('s') or '').strip(),
                  media_md5=(values.get('m') or '').split()[0] if values.get('m') else None,
                  reachable=bool(values.get('s')))
    return report


def status() -> dict:
    result = shell('echo UP=$(cat /proc/uptime);ls %s;wc -c<%s' % (VIDEO_DIR, CFG),
                   'ST', 'flow-status', 'read-only status of the boot video setup')
    return result


def doctor() -> dict:
    """One read-only frame that answers everything needed before changing anything.

    After a re-plug this is the first thing to run: it reports the channel, the
    boot config digest, both media directories, and whether the external picture
    viewer exists at all.
    """
    first = shell('echo U=$(cat /proc/uptime);echo F=$(command -v fbviewer);echo C=$(md5sum<%s)' % CFG,
                  'DR', 'flow-doctor-1',
                  'read-only doctor part 1: uptime, fbviewer path and the config digest')
    second = shell('echo VD;ls %s;echo ID;ls %s 2>/dev/null||echo MISSING' % (VIDEO_DIR, IMG_DIR),
                   'DS', 'flow-doctor-2',
                   'read-only doctor part 2: what the video and picture directories contain')

    # These echoes are not token-prefixed (the body has no room for that once the
    # two paths are in it), so parse KEY=VALUE out of every line, tagged or bare.
    values = {}
    for line in _lines(first):
        key, sep, value = line.partition('=')
        if sep and len(key) <= 2:
            values[key.strip().lower()] = value.strip()

    report = dict(state=first['state'], uptime=values.get('u'), fbviewer=values.get('f'),
                  config_md5=(values.get('c') or '').split()[0] if values.get('c') else None,
                  bare=first['bare'] + second['bare'])
    report['config_is_factory'] = report['config_md5'] == ORIGINAL_CFG_MD5
    report['video_dir_present'] = any('.mp4' in line for line in second['bare'])
    report['image_dir_present'] = not any('MISSING' in line for line in second['bare'])
    report['picture_mode_possible'] = bool(report['fbviewer'])
    return report
