"""Use only tmp_path; never user configuration, GUI, serial, or USB."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

import dashboard_settings as settings


@pytest.mark.parametrize('bad', [None, True, [], {}, 'bad', 'NaN', 'Infinity', -1, 0.49, 61, 10**400])
def test_bad_interval_preserves_other_valid_fields(tmp_path, bad):
    path = tmp_path / 'settings.json'
    original = dict(interval=bad, theme='琥珀', mode='图片', picture='D:/照片.png', media='D:/片段.mp4')
    path.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
    result = settings.load_settings(path)
    expected = dict(settings.DEFAULTS)
    expected.update(original)
    expected['interval'] = 1.0
    assert result == expected


@pytest.mark.parametrize('bad', ['{bad', '[]', 'null', '0', 'true', '"text"'])
def test_bad_document_returns_independent_defaults(tmp_path, bad):
    path = tmp_path / 'settings.json'
    path.write_text(bad, encoding='utf-8')
    result = settings.load_settings(path)
    assert result == settings.DEFAULTS
    result['theme'] = 'changed'
    assert settings.DEFAULTS['theme'] == '青蓝'


def test_missing_bad_utf8_and_bom(tmp_path):
    path = tmp_path / 'settings.json'
    assert settings.load_settings(path) == settings.DEFAULTS
    path.write_bytes(b'\xff\xfe')
    assert settings.load_settings(path) == settings.DEFAULTS
    path.write_text(json.dumps(dict(theme='琥珀')), encoding='utf-8-sig')
    assert settings.load_settings(path)['theme'] == '琥珀'


@pytest.mark.parametrize('key,value', [('theme', []), ('mode', 'unknown'),
                                       ('picture', 123), ('media', 'bad\0path')])
def test_invalid_field_isolated_on_load_and_rejected_on_save(tmp_path, key, value):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(dict(interval=2, **{key: value})), encoding='utf-8')
    assert settings.load_settings(path) == dict(settings.DEFAULTS, interval=2)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=key):
        settings.save_settings({key: value}, path)
    assert path.read_bytes() == before


def test_valid_roundtrip_preserves_unrelated_tmp_and_no_temporary_remains(tmp_path):
    path = tmp_path / '配置/settings.json'
    path.parent.mkdir()
    unrelated = path.with_suffix('.tmp')
    unrelated.write_bytes(b'unrelated user file')
    expected = dict(settings.DEFAULTS, interval=0.5, theme='琥珀', mode='GIF / 视频',
                    picture='D:/照片.png', media='D:/视频.mp4', pictures=['D:/一.png', 'D:/二.png'])
    settings.save_settings(expected, path)
    assert settings.load_settings(path) == expected
    assert unrelated.read_bytes() == b'unrelated user file'
    assert sorted(p.name for p in path.parent.iterdir()) == ['settings.json', 'settings.tmp']


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_write_failure_preserves_old_config_and_removes_only_own_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / 'settings.json'
    old = b'{"interval": 5}'
    path.write_bytes(old)
    unrelated = tmp_path / 'settings.tmp'
    unrelated.write_bytes(b'keep')
    def fail(*args, **kwargs):
        raise OSError('injected failure')
    if failure == 'fsync':
        monkeypatch.setattr(settings.os, 'fsync', fail)
    else:
        monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError, match='injected'):
        settings.save_settings({'interval': 2}, path)
    assert path.read_bytes() == old
    assert unrelated.read_bytes() == b'keep'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['settings.json', 'settings.tmp']


def test_invalid_save_touches_no_directory(tmp_path):
    path = tmp_path / 'absent/settings.json'
    with pytest.raises(ValueError):
        settings.save_settings({'interval': float('nan')}, path)
    assert not path.parent.exists()


def test_partial_temporary_write_never_replaces_old_config(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'{"interval": 5}')
    create = settings.tempfile.NamedTemporaryFile
    class BrokenWrite:
        def __init__(self, **kwargs):
            self.inner = create(**kwargs)
            self.name = self.inner.name
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.inner.close()
        def write(self, payload):
            self.inner.write(payload[:5])
            raise OSError('partial temporary write')
    monkeypatch.setattr(settings.tempfile, 'NamedTemporaryFile', BrokenWrite)
    with pytest.raises(OSError, match='partial'):
        settings.save_settings({'interval': 2}, path)
    assert path.read_bytes() == b'{"interval": 5}'
    assert list(tmp_path.iterdir()) == [path]


def test_flush_to_disk_precedes_replace(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    operations = []
    sync = settings.os.fsync
    replace = Path.replace
    def tracked_sync(fd):
        operations.append('sync')
        sync(fd)
    def tracked_replace(source, target):
        assert operations == ['sync']
        assert json.loads(source.read_text(encoding='utf-8'))['interval'] == 2
        operations.append('replace')
        return replace(source, target)
    monkeypatch.setattr(settings.os, 'fsync', tracked_sync)
    monkeypatch.setattr(Path, 'replace', tracked_replace)
    settings.save_settings({'interval': 2}, path)
    assert operations == ['sync', 'replace']


def test_gui_persist_reports_failure_without_claiming_success(tmp_path, monkeypatch):
    # Exercise the actual callback, but create no Tk window or image and import
    # no user settings: the object has only the values persist() consumes.
    from types import SimpleNamespace
    import dashboard_app
    path = tmp_path / 'settings.json'
    path.write_bytes(b'{"interval": 5}')
    messages = []
    obj = SimpleNamespace(interval_seconds=lambda: 2,
        theme=SimpleNamespace(get=lambda: '琥珀'), mode=SimpleNamespace(get=lambda: '图片'),
        picture_path='D:/照片.png', picture_paths=['D:/照片.png'], media_path='',
        desktop_broker=SimpleNamespace(get=lambda: False),
        desktop_differ=SimpleNamespace(get=lambda: False),
        desktop_fps=SimpleNamespace(get=lambda: '15'),
        rotate_media=SimpleNamespace(get=lambda: True),
        onboard_kind=SimpleNamespace(get=lambda: 'video'),
        onboard_fit=SimpleNamespace(get=lambda: 'blur'),
        onboard_rotate=SimpleNamespace(get=lambda: True),
        onboard_reboot=SimpleNamespace(get=lambda: False),
        onboard_show=SimpleNamespace(get=lambda: True),
        onboard_target=SimpleNamespace(get=lambda: '60'),
        status=SimpleNamespace(set=messages.append))
    def save(value):
        return settings.save_settings(value, path)
    monkeypatch.setattr(dashboard_app, 'save_settings', save)
    monkeypatch.setattr(Path, 'replace', lambda *args: (_ for _ in ()).throw(OSError('locked')))
    assert dashboard_app.DashboardApp.persist(obj) is False
    assert messages[-1].startswith('设置未保存：')
    assert path.read_bytes() == b'{"interval": 5}'


def test_concurrent_saves_use_distinct_files_and_never_mix_json(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    barrier = threading.Barrier(2)
    replace = Path.replace
    sources = []
    def synchronized(source, target):
        sources.append(source)
        barrier.wait(timeout=5)
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', synchronized)
    values = [dict(settings.DEFAULTS, interval=2, theme='琥珀'),
              dict(settings.DEFAULTS, interval=5, mode='测试画面')]
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(settings.save_settings, value, path) for value in values]
        completed = []
        for future in futures:
            try:
                future.result(timeout=10)
                completed.append(futures.index(future))
            except PermissionError:
                # Windows may deny one overlapping replace. The surfaced error
                # must not damage the successful writer's complete document.
                pass
    assert len(set(sources)) == 2
    assert completed
    assert settings.load_settings(path) in [values[i] for i in completed]
    assert list(tmp_path.iterdir()) == [path]
