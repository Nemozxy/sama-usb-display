from pathlib import Path
import subprocess

import pytest
from PIL import Image

import onboard_media


def test_onboard_rotation_is_clockwise_for_images(tmp_path):
    source = Image.new('RGB', (40, 20), 'red')
    for x in range(20, 40):
        for y in range(20):
            source.putpixel((x, y), (0, 0, 255))
    path = tmp_path / 'landscape.png'
    source.save(path)
    result = onboard_media.prepare_image(path, tmp_path / 'out', fit='pad', rotate=True)
    with Image.open(result['path']) as prepared:
        assert prepared.size == (720, 1568)
        assert prepared.getpixel((360, 100)) == (255, 0, 0)
        assert prepared.getpixel((360, 1460)) == (0, 0, 255)


@pytest.mark.parametrize('fit', ['pad', 'fill', 'blur'])
def test_all_onboard_video_fit_modes_share_one_rotation(fit):
    simple, complex_filter, _ = onboard_media._video_filters(fit, rotate=True)
    value = complex_filter or simple
    assert value.count('transpose=clock') == 1


def test_real_onboard_video_preparation(tmp_path):
    if not Path(onboard_media.FFMPEG).is_file() or not Path(onboard_media.FFPROBE).is_file():
        pytest.skip('FFmpeg/ffprobe are not installed')
    source = tmp_path / 'landscape.mp4'
    subprocess.run([onboard_media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'color=c=blue:s=32x16:r=2', '-t', '0.2',
        '-pix_fmt', 'yuv420p', str(source)], check=True, timeout=10)
    progress = []
    result = onboard_media.prepare(source, tmp_path / 'prepared', fit='pad', rotate=True,
        progress=progress.append)
    assert progress and progress[-1] == 1.0
    assert all(0 <= value <= 1 for value in progress)
    assert all(a <= b for a, b in zip(progress, progress[1:]))
    assert result['action'] == 'transcoded'
    assert result['info']['profile'] == 'High'
    assert int(result['info']['level']) == 32
    assert result['info']['width'] == 720 and result['info']['height'] == 1568
    assert result['info']['pix_fmt'] == 'yuv420p'
    assert result['moov_at_end'] and result['size'] <= onboard_media.MEDIA_MAX


def test_two_pass_target_size_commands_and_progress(tmp_path, monkeypatch):
    commands = []
    progress = []
    monkeypatch.setattr(onboard_media, '_run_ffmpeg',
        lambda command, callback, duration: (commands.append(command), callback and callback(1.0)))
    monkeypatch.setattr(onboard_media, 'probe', lambda path: {})
    onboard_media.transcode(tmp_path / 'in.mp4', tmp_path / 'out.mp4',
        duration=10, target_bytes=1024 * 1024, progress=progress.append)
    assert len(commands) == 2
    assert [command[command.index('-pass') + 1] for command in commands] == ['1', '2']
    assert all(command[command.index('-b:v') + 1] == '796917' for command in commands)
    assert commands[0][-2:] == ['-f', 'null'] or commands[0][-3:-1] == ['-f', 'null']
    assert commands[1][-1].endswith('out.mp4')
    assert progress == [0.5, 1.0]


def test_impossible_target_is_rejected_before_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard_media, '_run_ffmpeg',
        lambda *args: pytest.fail('FFmpeg must not start'))
    with pytest.raises(ValueError, match='too small'):
        onboard_media.transcode(tmp_path / 'in.mp4', tmp_path / 'out.mp4',
            duration=10000, target_bytes=1024 * 1024)
