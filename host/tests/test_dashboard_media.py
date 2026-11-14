import subprocess
import time

import pytest
from PIL import Image

import dashboard_media


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_gif_loops_and_stops_cleanly(tmp_path):
    path = tmp_path / 'loop.gif'
    frames = [Image.new('RGB', (24, 24), color) for color in ('red', 'blue')]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    player = dashboard_media.MediaPlayer(path, fps=4)
    player.start()
    try:
        wait_for(lambda: player.loops >= 1 and player.decoded >= 3)
    finally:
        assert player.stop()
    assert player.error is None and player.decoded >= 3


def test_real_ffmpeg_video_loops_and_stops(tmp_path, monkeypatch):
    try:
        ffmpeg = dashboard_media.find_ffmpeg()
    except RuntimeError:
        pytest.skip('FFmpeg is not installed')
    path = tmp_path / 'loop.mp4'
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'color=c=blue:s=32x16:r=2', '-t', '0.2',
        '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=10)
    commands = []
    real_popen = dashboard_media.subprocess.Popen
    def record(command, **kwargs):
        commands.append(command)
        return real_popen(command, **kwargs)
    monkeypatch.setattr(dashboard_media.subprocess, 'Popen', record)
    player = dashboard_media.MediaPlayer(path, fps=2)
    player.start()
    try:
        wait_for(lambda: player.decoded >= 2)
    finally:
        assert player.stop()
    assert player.error is None
    command = next(item for item in commands if '-stream_loop' in item)
    assert command[command.index('-stream_loop') + 1] == '-1'
    assert 'transpose=clock' in command[command.index('-vf') + 1]


def test_landscape_rotation_is_clockwise_and_optional():
    source = Image.new('RGB', (40, 20), 'red')
    for x in range(20, 40):
        for y in range(20):
            source.putpixel((x, y), (0, 0, 255))
    rotated = dashboard_media.fit_frame(source, rotate_landscape=True)
    assert rotated.getpixel((360, 200)) == (255, 0, 0)
    assert rotated.getpixel((360, 1360)) == (0, 0, 255)
    unrotated = dashboard_media.fit_frame(source, rotate_landscape=False)
    assert unrotated.getpixel((200, 780)) == (255, 0, 0)
    assert unrotated.getpixel((500, 780)) == (0, 0, 255)


def test_repeated_gif_replacement_releases_players_frames_and_threads(tmp_path):
    import gc
    import weakref

    path = tmp_path / 'loop.gif'
    Image.new('RGB', (24, 24), 'green').save(path)
    references = []
    for _ in range(8):
        player = dashboard_media.MediaPlayer(path)
        player.start()
        wait_for(lambda: player.decoded >= 1)
        frame = player.take()
        references.append((weakref.ref(player), weakref.ref(frame)))
        assert player.stop() and not player.worker.is_alive()
        player = frame = None
    gc.collect()
    assert all(player() is None and frame() is None for player, frame in references)
    assert [path.name for path in tmp_path.iterdir()] == ['loop.gif']


def test_repeated_video_replacement_terminates_ffmpeg(tmp_path):
    import gc
    import weakref

    try:
        ffmpeg = dashboard_media.find_ffmpeg()
    except RuntimeError:
        pytest.skip('FFmpeg is not installed')
    path = tmp_path / 'loop.mp4'
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'color=c=blue:s=32x16:r=2', '-t', '0.2',
        '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=10)
    references = []
    for _ in range(3):
        player = dashboard_media.MediaPlayer(path, fps=2)
        player.start()
        wait_for(lambda: player.process is not None and player.decoded >= 1)
        process = player.process
        references.append(weakref.ref(player))
        assert player.stop() and process.poll() is not None
        assert player.process is None and not player.worker.is_alive()
        player = process = None
    gc.collect()
    assert all(player() is None for player in references)
    assert [path.name for path in tmp_path.iterdir()] == ['loop.mp4']
