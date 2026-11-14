"""A 主机媒体播放：顺序解码、容量一预览邮箱、不访问设备。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from dashboard_render import SIZE, BACKGROUND

DECODER_DIAGNOSTIC_LIMIT = 4096


class _DecoderDiagnostics:
    """Drain continuously so FFmpeg cannot block; retain only a bounded tail."""
    def __init__(self, process):
        self.process = process
        self.tail = bytearray()
        self.total = 0
        self.error = None
        self.worker = threading.Thread(target=self._run, name='sama-decoder-errors', daemon=False)

    def _run(self):
        try:
            while True:
                chunk = self.process.stderr.read(DECODER_DIAGNOSTIC_LIMIT)
                if not chunk:
                    break
                self.total += len(chunk)
                self.tail.extend(chunk)
                del self.tail[:-DECODER_DIAGNOSTIC_LIMIT]
        except Exception as exc:
            self.error = str(exc)[:DECODER_DIAGNOSTIC_LIMIT]
            # A failed drain must not leave the decoder blocked on stderr.
            try:
                self.process.kill()
            except OSError:
                pass

    def detail(self):
        # Only called after the decoder exits and the drain thread joins.
        if self.error is not None:
            return '解码器诊断读取失败：' + self.error
        return bytes(self.tail).decode('utf-8', 'replace').strip()


def find_ffmpeg():
    # 打包版优先使用随程序分发的同版本解码器。
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'ffmpeg.exe'
    found = str(bundled) if bundled.is_file() else shutil.which('ffmpeg')
    if not found:
        raise RuntimeError('未找到 FFmpeg。请使用含解码器的完整程序目录，或安装 FFmpeg 并加入 PATH。')
    return found


def video_dimensions(path):
    ffmpeg = Path(find_ffmpeg())
    bundled = ffmpeg.with_name('ffprobe.exe')
    ffprobe = str(bundled) if bundled.is_file() else shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('横屏旋转需要 ffprobe；请安装完整 FFmpeg 工具集并加入 PATH。')
    completed = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height', '-of', 'json', str(path)],
        capture_output=True, timeout=10)
    if completed.returncode:
        raise RuntimeError('无法读取视频尺寸：' + completed.stderr.decode('utf-8', 'replace')[:512])
    import json
    streams = json.loads(completed.stdout).get('streams', [])
    if not streams:
        raise ValueError('视频没有可显示的画面流')
    width, height = int(streams[0]['width']), int(streams[0]['height'])
    if width <= 0 or height <= 0:
        raise ValueError('视频尺寸无效')
    return width, height


def fit_frame(image, rotate_landscape=True):
    rgba = image.convert('RGBA')
    if rotate_landscape and rgba.width > rgba.height:
        rgba = rgba.transpose(Image.Transpose.ROTATE_270)
    background = Image.new('RGBA', rgba.size, BACKGROUND)
    background.alpha_composite(rgba)
    fitted = ImageOps.contain(background.convert('RGB'), (688, 1472), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', SIZE, BACKGROUND)
    canvas.paste(fitted, ((720 - fitted.width) // 2, 38 + (1472 - fitted.height) // 2))
    return canvas


class MediaPlayer:
    """解码器与 GUI 分离；仅保留最新帧，不累积完整视频或 GIF。"""
    def __init__(self, path, *, fps=4, notify=None, rotate_landscape=True):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('媒体文件不存在')
        if not 1 <= fps <= 10:
            raise ValueError('媒体预览帧率须为 1–10')
        self.fps = fps
        self.rotate_landscape = rotate_landscape
        self.notify = notify or (lambda kind, value: None)
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.process = None
        self.decoded = self.replaced = self.loops = 0
        self.error = None
        self.decoder_stderr_bytes = 0
        self.decoder_stderr_retained_bytes = 0
        self.worker = threading.Thread(target=self._run, name='sama-media', daemon=False)

    def start(self):
        self.worker.start()

    def take(self):
        with self.lock:
            frame, self.latest = self.latest, None
            return frame

    def stop(self, wait_s=5):
        self.halt.set()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self.worker.ident is not None:
            self.worker.join(wait_s)
        return not self.worker.is_alive()

    def _publish(self, image):
        with self.lock:
            self.replaced += self.latest is not None
            self.latest = image
            self.decoded += 1

    def _gif(self):
        with Image.open(self.path) as source:
            if source.width * source.height > 16_000_000:
                raise ValueError('GIF 尺寸过大，请先缩小至 1600 万像素以内')
            index = 0
            deadline = time.monotonic()
            while not self.halt.is_set():
                try:
                    source.seek(index)
                except EOFError:
                    source.seek(0)
                    index = 0
                    self.loops += 1
                # Pillow 顺序 seek 负责 GIF disposal/透明叠帧。
                delay = max(0.1, float(source.info.get('duration', 100)) / 1000)
                self._publish(fit_frame(source, self.rotate_landscape))
                index += 1
                deadline = max(deadline + delay, time.monotonic())
                if self.halt.wait(max(0, deadline - time.monotonic())):
                    break

    def _video(self):
        filters = []
        if self.rotate_landscape:
            width, height = video_dimensions(self.path)
            if width > height:
                filters.append('transpose=clock')
        filters.extend((f'fps={self.fps}',
            'scale=688:1472:force_original_aspect_ratio=decrease',
            'pad=720:1568:(ow-iw)/2:38+(1472-ih)/2:color=0x101827', 'setsar=1'))
        vf = ','.join(filters)
        command = [find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin',
                   '-stream_loop', '-1', '-threads', '2', '-i', str(self.path),
                   '-map', '0:v:0', '-an', '-sn', '-dn', '-filter_threads', '1',
                   '-vf', vf, '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
        incomplete = False
        progress = [time.monotonic()]
        watchdog_stop = threading.Event()
        stalled = threading.Event()
        diagnostics = watchdog = None
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def watch():
            while not watchdog_stop.wait(.25):
                if process.poll() is not None or self.halt.is_set():
                    return
                if time.monotonic() - progress[0] > 10:
                    stalled.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        try:
            # Publish child ownership before any helper can fail to construct.
            # stop() must see it even during partial decoder initialization.
            with self.lock:
                self.process = process
            diagnostics = _DecoderDiagnostics(process)
            watchdog = threading.Thread(target=watch, name='sama-decoder-watch', daemon=True)
            diagnostics.worker.start()
            watchdog.start()
            if self.halt.is_set():
                return
            size = SIZE[0] * SIZE[1] * 3
            deadline = time.monotonic()
            while not self.halt.is_set():
                raw = bytearray()
                while len(raw) < size and not self.halt.is_set():
                    chunk = process.stdout.read(size - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if self.halt.is_set():
                    break
                if len(raw) != size:
                    incomplete = True
                    break
                self._publish(Image.frombytes('RGB', SIZE, bytes(raw)))
                progress[0] = time.monotonic()
                deadline = max(deadline + 1 / self.fps, time.monotonic())
                if self.halt.wait(max(0, deadline - time.monotonic())):
                    break
        finally:
            watchdog_stop.set()
            try:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        if process.poll() is None:
                            raise
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait()
            finally:
                process.stdout.close()
                if diagnostics is not None and diagnostics.worker.ident is not None:
                    diagnostics.worker.join()
                process.stderr.close()
                if watchdog is not None and watchdog.ident is not None:
                    watchdog.join()
                if diagnostics is not None:
                    self.decoder_stderr_bytes = diagnostics.total
                    self.decoder_stderr_retained_bytes = len(diagnostics.tail)
                with self.lock:
                    self.process = None
        if incomplete and not self.halt.is_set():
            if stalled.is_set():
                raise RuntimeError('视频解码超过 10 秒未产生画面，已停止解码器')
            raise RuntimeError('视频解码提前结束：' + (diagnostics.detail() or '无可解码画面 / 文件损坏'))
        if diagnostics.error is not None and not self.halt.is_set():
            raise RuntimeError(diagnostics.detail())

    def _run(self):
        try:
            if self.path.suffix.lower() == '.gif':
                self._gif()
            else:
                self._video()
        except Exception as exc:
            if not self.halt.is_set():
                self.error = str(exc)
                self.notify('media_error', self.error)
        finally:
            self.notify('media_stopped', dict(decoded=self.decoded, replaced=self.replaced,
                decoder_stderr_bytes=self.decoder_stderr_bytes,
                decoder_stderr_retained_bytes=self.decoder_stderr_retained_bytes))
