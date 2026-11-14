import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import onboard_flow as flow

REAL_STOP_VIDEO = flow.stop_video


@pytest.fixture(autouse=True)
def no_real_port(monkeypatch, tmp_path):
    monkeypatch.setattr(flow, '_SESSION', object())
    monkeypatch.setattr(flow, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(flow, 'stop_video', lambda: {'requested': True, 'playback_verified': False})


def reply(fields=None, bare=None):
    return dict(state='OK', fields=fields or {}, tagged=[], bare=bare or [],
                note='NO_VENDOR_BLOCK', rx_bytes=10)


def test_redirect_variables_resolve_to_allowed_paths():
    command = flow.wrap('t=/mnt/UDISK/video/.sama-next;cat /tmp/suv>$t;wc -c<$t', 'CP')
    assert flow._validated_redirects(command, (flow.VIDEO_NEXT,)) == [
        flow.VIDEO_NEXT, '/dev/ttyGS0']
    with pytest.raises(RuntimeError, match='outside the allowlist'):
        flow._validated_redirects(command)
    with pytest.raises(RuntimeError, match='unbound'):
        flow._validated_redirects(flow.wrap('cat /tmp/suv>$t', 'CP'), (flow.VIDEO_NEXT,))


def test_config_layout_and_template_are_fixed():
    data = flow.build_config('/mnt/UDISK/video/sama-own.mp4')
    assert len(data) == 132 and data[0] == 0x80 and data[1] == 2
    assert data[5:].split(b'\0')[0] == b'/mnt/UDISK/video/sama-own.mp4'
    assert hashlib.md5(flow.TEMPLATE.read_bytes()).hexdigest() == flow.ORIGINAL_CFG_MD5
    with pytest.raises(RuntimeError):
        flow.build_config('/mnt/UDISK/video/' + 'x' * 200)


def stub_video_push(monkeypatch, tmp_path, target_md5=None, commit_md5=None,
                    copy_rc='0', sync_rc='0'):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'video bytes')
    digest = hashlib.md5(source.read_bytes()).hexdigest()
    calls = []

    def fake_shell(body, token, grant, purpose, allow_write=(), session=None):
        calls.append((token, body, allow_write))
        if token in ('OK', 'KP'):
            return reply(fields={'own': 'present', 'rc': '0', 'sync': '0'})
        if token == 'VR':
            return reply(bare=['11', digest + '  -'])
        if token == 'CP':
            return reply(fields={'rc': copy_rc, 'sync': sync_rc},
                         bare=['11', (target_md5 or digest) + '  -'])
        if token == 'CM':
            return reply(fields={'rc': copy_rc, 'sync': sync_rc},
                         bare=['11', (commit_md5 or digest) + '  -'])
        raise AssertionError(token)

    monkeypatch.setattr(flow, 'shell', fake_shell)
    monkeypatch.setattr(flow, '_bulk_send', lambda *args, **kwargs: {'frames': 1})
    return source, digest, calls


def test_video_replacement_uses_fixed_current_and_backup_slots(monkeypatch, tmp_path):
    source, digest, calls = stub_video_push(monkeypatch, tmp_path)
    stages = []
    result = flow.push_video(source, step=stages.append)
    assert stages == ['backup', 'upload', 'verify', 'stage', 'commit']
    assert result['device_path'] == '/mnt/UDISK/video/sama-own.mp4'
    assert result['verified_on_tmp'] and result['staged_and_verified']
    assert result['copied_and_verified']
    assert result['kept_previous'] and not result['combined_shell']
    assert [token for token, body, allowed in calls] == ['KP', 'VR', 'CP', 'CM']
    assert flow.VIDEO_BACKUP == '/mnt/UDISK/vprev'
    assert 'sync' in calls[2][1] and 'mv -f' in calls[3][1]
    assert all(len(flow.wrap(body, token).encode('ascii')) <= flow.CAP
               for token, body, allowed in calls)


def test_bad_staging_does_not_replace_active_video(monkeypatch, tmp_path):
    source, digest, calls = stub_video_push(monkeypatch, tmp_path, target_md5='0' * 32)
    result = flow.push_video(source)
    assert result['verified_on_tmp'] and not result['staged_and_verified']
    assert not result['copied_and_verified']
    assert [token for token, body, allowed in calls] == ['KP', 'VR', 'CP']


def test_bad_commit_digest_does_not_pass_verification(monkeypatch, tmp_path):
    source, digest, calls = stub_video_push(monkeypatch, tmp_path, commit_md5='0' * 32)
    result = flow.push_video(source)
    assert result['staged_and_verified'] and not result['copied_and_verified']


def test_sync_failure_does_not_pass_verification(monkeypatch, tmp_path):
    source, digest, calls = stub_video_push(monkeypatch, tmp_path, sync_rc='1')
    result = flow.push_video(source)
    assert not result['staged_and_verified'] and not result['copied_and_verified']


def test_apply_config_writes_only_after_bulk_digest(monkeypatch):
    data = flow.build_config('/mnt/UDISK/video/x.mp4')
    digest = hashlib.md5(data).hexdigest()
    replies = [reply(fields={'own': 'present'}),
               reply(fields={'rc': '0', 'sync': '0'}, bare=['132', digest + '  -'])]
    monkeypatch.setattr(flow, '_bulk_send', lambda *args, **kwargs: {'frames': 2})
    monkeypatch.setattr(flow, 'shell', lambda *args, **kwargs: replies.pop(0))
    assert flow.apply_config('/mnt/UDISK/video/x.mp4')['written']


def test_rollback_uses_verified_synced_atomic_replacement(monkeypatch):
    calls = []

    def fake_shell(body, token, grant, purpose, allow_write=(), session=None):
        calls.append(token)
        if token == 'RV':
            return reply(bare=['11', 'a' * 32 + '  -'])
        return reply(fields={'rc': '0', 'sync': '0'}, bare=['11', 'a' * 32 + '  -'])

    monkeypatch.setattr(flow, 'shell', fake_shell)
    result = flow.rollback_video()
    assert result['rolled_back'] and result['device_md5'] == 'a' * 32
    assert calls == ['RV', 'CP', 'CM']


def test_rollback_without_previous_does_not_write(monkeypatch):
    monkeypatch.setattr(flow, 'shell', lambda *args, **kwargs: reply(bare=['NO_PREVIOUS']))
    assert not flow.rollback_video()['rolled_back']


def test_image_replacement_uses_one_fixed_png_and_saves_video_config(monkeypatch, tmp_path):
    source = tmp_path / 'image.png'
    source.write_bytes(b'png bytes')
    digest = hashlib.md5(source.read_bytes()).hexdigest()
    bodies = []

    def fake_shell(body, token, grant, purpose, allow_write=(), session=None):
        bodies.append(body)
        if token == 'FB':
            return reply(bare=['/bin/fbviewer'])
        if token in ('CK', 'CM'):
            return reply(fields={'rc': '0', 'sync': '0'},
                         bare=[str(source.stat().st_size), digest + '  -'])
        return reply(fields={'own': 'present'})

    monkeypatch.setattr(flow, 'shell', fake_shell)
    monkeypatch.setattr(flow, '_bulk_send', lambda *args, **kwargs: {'frames': 2})
    result = flow.push_image(source)
    assert result['ok'] and result['copied_and_verified']
    assert result['device_path'] == '/mnt/UDISK/img/s.png'
    assert any(flow.VIDEO_CFG_COPY in body for body in bodies)
    assert any('mv -f' in body and 'sync' in body for body in bodies)
    assert not any('unlink' in body or 'rm -rf' in body for body in bodies)


class FakePort:
    def __init__(self, short=False):
        self.writes = []
        self.short = short
        self.replies = [b'create_success', flow.DONE_MARKER]

    def write(self, data):
        self.writes.append(data)
        return len(data) - 1 if self.short and len(self.writes) > 1 else len(data)

    def flush(self):
        pass

    def read(self, size):
        return self.replies.pop(0) if self.replies else b''


def test_cmd111_stream_is_bounded_and_byte_exact(monkeypatch):
    port = FakePort()
    monkeypatch.setattr(flow, '_SESSION', SimpleNamespace(port=port, frames=0))
    progress = []
    result = flow._bulk_send(b'abc', '/tmp/suv', 'test', already_created=True,
        progress=progress.append)
    assert result['frames'] == 2 and port.writes[0][0] == 111
    assert port.writes[1] == b'abc' + bytes(247)
    assert flow._SESSION.frames == 2
    assert progress == [0.0, 0.99, 1.0]


def test_cmd111_short_write_stops_without_retry(monkeypatch):
    port = FakePort(short=True)
    monkeypatch.setattr(flow, '_SESSION', SimpleNamespace(port=port, frames=0))
    with pytest.raises(OSError) as stopped:
        flow._bulk_send(b'abc', '/tmp/suv', 'test', already_created=True)
    assert len(port.writes) == 2
    detail = str(stopped.value)
    assert 'batch=0' in detail and 'data_offset=0' in detail and 'total_bytes=3' in detail


def test_cmd111_drains_pending_status_after_data_batch(monkeypatch):
    class StatusPort(FakePort):
        def __init__(self):
            super().__init__()
            self.status_pending = False

        @property
        def in_waiting(self):
            return 4 if self.status_pending else 0

        def write(self, data):
            result = super().write(data)
            if len(self.writes) == 2:
                self.status_pending = True
            return result

        def read(self, size):
            if self.status_pending:
                self.status_pending = False
                return b'stat'
            return super().read(size)

    port = StatusPort()
    monkeypatch.setattr(flow, '_SESSION', SimpleNamespace(port=port, frames=0))
    result = flow._bulk_send(b'abc', '/tmp/suv', 'test', already_created=True)
    assert result['status_bytes'] == 4


def test_stop_video_uses_cmd121_in_current_session(monkeypatch):
    port = FakePort()
    monkeypatch.setattr(flow, '_SESSION', SimpleNamespace(port=port, frames=0))
    monkeypatch.setattr(flow, '_settle', lambda port, seconds: 0)
    monkeypatch.setattr(flow, 'stop_video', REAL_STOP_VIDEO)
    result = flow.stop_video()
    assert port.writes[0][0] == 121 and len(port.writes[0]) == 250
    assert flow._SESSION.frames == 1
    assert result['requested'] and not result['playback_verified']


def test_stop_failure_prevents_any_upload_step(monkeypatch, tmp_path):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'video')
    monkeypatch.setattr(flow, '_SESSION', object())
    monkeypatch.setattr(flow, 'stop_video', lambda: (_ for _ in ()).throw(OSError('stop failed')))
    monkeypatch.setattr(flow, 'shell', lambda *args, **kwargs: pytest.fail('shell must not run'))
    monkeypatch.setattr(flow, '_bulk_send', lambda *args, **kwargs: pytest.fail('upload must not start'))
    with pytest.raises(OSError, match='stop failed'):
        flow.push_video(source)
