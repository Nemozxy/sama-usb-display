"""One-click "make this file the boot video" for the SAMA panel.

WHAT IT WRAPS
    Three steps that were each verified by hand on 2026-10-24:
      1. transcode to the shape the factory player accepts
         (H.264 High profile, level 3.2, ref=3, cabac, b-frames, moov at the END);
      2. push the bytes over the factory's cmd111 bulk channel to /tmp/suv;
      3. copy to /mnt/UDISK/video/ and point the 132-byte /app.cfg at it.

WHAT IT REFUSES TO DO
    It never touches NAND/MTI/UBI raw nodes, the boot chain, or the factory
    service. It reads and writes exactly two persistent paths, both of which the
    operator approved for this class of work: /mnt/UDISK/video/ and /app.cfg.

WHY THE TRANSCODE IS NOT OPTIONAL
    A Baseline-profile file with moov at the front made the panel stall on its
    boot logo (2026-10-24). Matching the factory file's profile, level and moov
    placement fixed it. Choosing "nice" encoder settings is therefore a
    correctness requirement here, not taste.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = '/mnt/UDISK/video'
CFG = '/app.cfg'
CFG_BACKUP = '/mnt/UDISK/app.cfg.orig'
TMP_TARGET = '/tmp/suv'
MEDIA_MAX = 64 * 1024 * 1024
TARGET_MEDIA_BYTES = 60 * 1024 * 1024
# 2026-10-25: a 17.4 MB clip transferred intact, so the\
                             # old 1 MB 'innerFileBufLen' figure was a buffer size, not a cap.
FFMPEG = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE = shutil.which('ffprobe') or 'ffprobe'
WIDTH, HEIGHT = 720, 1568


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------

def probe(path: Path) -> dict:
    command = [FFPROBE, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=codec_name,profile,level,width,height,pix_fmt,nb_frames',
               '-show_entries', 'format=format_name,duration,size',
               '-of', 'json', str(path)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError('ffprobe failed: %s' % done.stderr.strip())
    payload = json.loads(done.stdout)
    stream = (payload.get('streams') or [{}])[0]
    fmt = payload.get('format') or {}
    return dict(codec=stream.get('codec_name'), profile=stream.get('profile'),
                level=stream.get('level'), width=stream.get('width'),
                height=stream.get('height'), pix_fmt=stream.get('pix_fmt'),
                frames=stream.get('nb_frames'), format=fmt.get('format_name'),
                duration=fmt.get('duration'), size=fmt.get('size'))


def already_matching(info: dict) -> bool:
    return (info.get('codec') == 'h264' and info.get('profile') == 'High'
            and int(info.get('level') or 0) == 32
            and info.get('width') == WIDTH and info.get('height') == HEIGHT
            and info.get('pix_fmt') == 'yuv420p')


def moov_at_end(path: Path) -> bool:
    data = path.read_bytes()
    return data.find(b'mdat') < data.find(b'moov')


def _video_filters(fit: str, rotate: bool = False):
    prefix = 'transpose=clock,' if rotate else ''
    pad = (prefix + 'scale=%d:%d:force_original_aspect_ratio=decrease,'
           'pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1' % (WIDTH, HEIGHT, WIDTH, HEIGHT))
    fill = (prefix + 'scale=%d:%d:force_original_aspect_ratio=increase,'
            'crop=%d:%d,setsar=1' % (WIDTH, HEIGHT, WIDTH, HEIGHT))
    blur = ('[0:v]' + prefix + 'split=2[bg][fg];'
            '[bg]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,'
            'gblur=sigma=36,eq=brightness=-0.15[b];'
            '[fg]scale=%d:%d:force_original_aspect_ratio=decrease[f];'
            '[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[v]'
            % (WIDTH, HEIGHT, WIDTH, HEIGHT, WIDTH, HEIGHT))
    if fit == 'blur':
        return None, blur, ['-map', '[v]']
    if fit == 'fill':
        return fill, None, []
    return pad, None, []


def _run_ffmpeg(command, progress=None, duration=None):
    if progress is not None:
        command = command[:1] + ['-progress', 'pipe:1', '-nostats'] + command[1:]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    diagnostic = []

    def drain_error():
        diagnostic.append(process.stderr.read())

    stderr_worker = __import__('threading').Thread(target=drain_error,
        name='sama-onboard-ffmpeg-errors', daemon=False)
    stderr_worker.start()
    try:
        for line in process.stdout:
            key, _, value = line.strip().partition('=')
            if progress is not None and duration and duration > 0:
                seconds = None
                if key in ('out_time_ms', 'out_time_us'):
                    try:
                        seconds = int(value) / 1_000_000
                    except ValueError:
                        pass
                elif key == 'out_time':
                    try:
                        hours, minutes, rest = value.split(':')
                        seconds = int(hours) * 3600 + int(minutes) * 60 + float(rest)
                    except ValueError:
                        pass
                if seconds is not None:
                    progress(max(0.0, min(0.99, seconds / duration)))
        try:
            code = process.wait(timeout=3600)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('ffmpeg exceeded the one-hour processing limit') from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        stderr_worker.join()
        process.stderr.close()
    if code:
        raise RuntimeError('ffmpeg failed: %s' % ''.join(diagnostic).strip())
    if progress is not None:
        progress(1.0)


def transcode(source: Path, destination: Path, fit: str = 'pad', crf: int = 24,
              rotate: bool = False, progress=None, duration: float | None = None,
              target_bytes: int | None = None) -> dict:
    """Encode with CRF or a duration-derived two-pass target bitrate."""
    if fit not in ('pad', 'fill', 'blur'):
        raise RuntimeError('fit must be pad, fill or blur')
    simple, complex_, extra = _video_filters(fit, rotate)
    base = [FFMPEG, '-y', '-loglevel', 'error', '-i', str(source), '-an', '-sn', '-dn']
    if complex_:
        base += ['-filter_complex', complex_]
    else:
        base += ['-vf', simple]
    base += extra + ['-r', '30', '-c:v', 'libx264',
                     '-profile:v', 'high', '-level', '3.2',
                     '-x264-params', 'ref=3:cabac=1:bframes=3:keyint=30',
                     '-pix_fmt', 'yuv420p']
    if target_bytes is None:
        _run_ffmpeg(base + ['-crf', str(crf), str(destination)], progress, duration)
    else:
        if not duration or duration <= 0:
            raise ValueError('target-size encoding needs a known positive duration')
        bitrate = int(target_bytes * 8 * 0.95 / duration)
        if bitrate < 50_000:
            raise ValueError('target size is too small for this video duration')
        passlog = str(destination.with_suffix('.passlog'))
        for pass_number in (1, 2):
            command = base + ['-b:v', str(bitrate), '-pass', str(pass_number),
                              '-passlogfile', passlog]
            if pass_number == 1:
                command += ['-f', 'null', os.devnull]
            else:
                command += [str(destination)]
            mapped = None if progress is None else lambda value, n=pass_number: progress((n - 1 + value) / 2)
            _run_ffmpeg(command, mapped, duration)
    return probe(destination)


def _blurred_background(image, width: int, height: int, blur: int = 36, dim: float = 0.72):
    """Cover-scaled copy, blurred and darkened, to sit behind the real picture.

    The panel is 720x1600 — much taller than any normal photo — so the choice is
    between cropping the sides or leaving black bars. This third option keeps the
    whole frame and fills the rest with a wash of its own colours.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    scale = max(width / image.width, height / image.height)
    cover = image.resize((max(width, int(image.width * scale)),
                          max(height, int(image.height * scale))), Image.LANCZOS)
    left = (cover.width - width) // 2
    top = (cover.height - height) // 2
    cover = cover.crop((left, top, left + width, top + height))
    cover = cover.filter(ImageFilter.GaussianBlur(blur))
    return ImageEnhance.Brightness(cover).enhance(dim)


def prepare_image(source: Path, workdir: Path, fit: str = 'pad', rotate: bool = False) -> dict:
    """Fit one still image onto the panel at 720x1568 and save it as PNG.

    fit='pad'  letterbox with black bars (never crops)
    fit='fill' cover the panel, cropping the overflow
    fit='blur' keep the whole frame, fill the rest with a blurred copy of itself
    """
    from PIL import Image
    if fit not in ('pad', 'fill', 'blur'):
        raise RuntimeError('fit must be pad, fill or blur')
    workdir.mkdir(parents=True, exist_ok=True)
    source = source.resolve()
    if not source.is_file():
        raise RuntimeError('no such file: %s' % source)
    out = workdir / (source.stem + '.boot.png')
    image = Image.open(source).convert('RGB')
    if rotate and image.width > image.height:
        image = image.transpose(Image.Transpose.ROTATE_270)

    if fit == 'fill':
        scale = max(WIDTH / image.width, HEIGHT / image.height)
        resized = image.resize((max(WIDTH, int(image.width * scale)),
                                max(HEIGHT, int(image.height * scale))), Image.LANCZOS)
        left = (resized.width - WIDTH) // 2
        top = (resized.height - HEIGHT) // 2
        canvas = resized.crop((left, top, left + WIDTH, top + HEIGHT))
    else:
        scale = min(WIDTH / image.width, HEIGHT / image.height)
        resized = image.resize((max(1, int(image.width * scale)),
                                max(1, int(image.height * scale))), Image.LANCZOS)
        if fit == 'blur':
            canvas = _blurred_background(image, WIDTH, HEIGHT)
        else:
            canvas = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
        canvas.paste(resized, ((WIDTH - resized.width) // 2, (HEIGHT - resized.height) // 2))

    canvas.save(out, optimize=True)
    data = out.read_bytes()
    return dict(action='fitted', fit=fit, path=out, size=len(data),
                md5=hashlib.md5(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest(),
                width=WIDTH, height=HEIGHT, source_size=[image.width, image.height])



def prepare(source: Path, workdir: Path, fit: str = 'pad', rotate: bool = False,
            progress=None, target_bytes: int = TARGET_MEDIA_BYTES) -> dict:
    """Prepare a video within the selected target size when transcoding."""
    workdir.mkdir(parents=True, exist_ok=True)
    source = source.resolve()
    if not source.is_file():
        raise RuntimeError('no such file: %s' % source)
    if not 1024 * 1024 <= target_bytes < MEDIA_MAX:
        raise ValueError('target size must be 1..63 MiB')
    info = probe(source)
    out = workdir / (source.stem + '.boot.mp4')
    rotate_source = rotate and int(info.get('width') or 0) > int(info.get('height') or 0)
    reusable = (already_matching(info) and fit == 'pad' and not rotate_source
                and source.stat().st_size <= target_bytes)
    if reusable and moov_at_end(source):
        shutil.copyfile(source, out)
        action = 'copied'
    elif reusable:
        command = [FFMPEG, '-y', '-loglevel', 'error', '-i', str(source), '-c', 'copy',
                   str(out)]
        done = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            raise RuntimeError('remux failed: %s' % done.stderr.strip())
        action = 'remuxed'
    else:
        try:
            duration = float(info.get('duration') or 0)
        except ValueError:
            duration = 0
        transcode(source, out, fit=fit, rotate=rotate_source,
                  progress=progress, duration=duration or None, target_bytes=target_bytes)
        action = 'transcoded'
    if progress is not None:
        progress(1.0)
    final = probe(out)
    size = out.stat().st_size
    if size > target_bytes:
        raise RuntimeError('encoded file is %d B, over the %d B target'
                           % (size, target_bytes))
    data = out.read_bytes()
    return dict(action=action, fit=fit, path=out, info=final, size=size,
                md5=hashlib.md5(data).hexdigest(),
                sha256=hashlib.sha256(data).hexdigest(),
                moov_at_end=data.find(b'mdat') < data.find(b'moov'))
