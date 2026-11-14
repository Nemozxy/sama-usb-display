"""主机端画面生成；只使用 Pillow，不接触设备。"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

SIZE = (720, 1568)
BACKGROUND = '#101827'


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False):
    candidates = [
        Path('C:/Windows/Fonts') / ('msyhbd.ttc' if bold else 'msyh.ttc'),
        Path('C:/Windows/Fonts') / ('segoeuib.ttf' if bold else 'segoeui.ttf'),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def test_card(now: datetime | None = None) -> Image.Image:
    """文字和识别标记均放在本台设备历史实测可见区内。"""
    now = now or datetime.now()
    image = Image.new('RGB', SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 38, 699, 1508), radius=30, outline='#4b6485', width=3)
    draw.text((54, 76), 'SAMA', font=font(42, True), fill='#67e8f9')
    draw.text((54, 140), 'OPEN SCREEN', font=font(66, True), fill='white')
    draw.text((54, 240), '我们的第一张自制画面', font=font(38, True), fill='#e2e8f0')
    draw.text((54, 308), '720 × 1568  /  USB 自定义显示', font=font(26), fill='#94a3b8')
    draw.rounded_rectangle((54, 390, 665, 650), radius=24, fill='#1e3046')
    draw.text((88, 422), now.strftime('%H:%M:%S'), font=font(91, True), fill='white')
    draw.text((90, 558), now.strftime('%Y / %m / %d'), font=font(34), fill='#67e8f9')
    draw.text((54, 710), '色彩检查', font=font(32, True), fill='white')
    for index, (label, color) in enumerate([('红 R', '#f44336'), ('绿 G', '#22c55e'), ('蓝 B', '#2563eb')]):
        x = 54 + index * 209
        draw.rounded_rectangle((x, 775, x + 193, 960), radius=18, fill=color)
        draw.text((x + 24, 846), label, font=font(30, True), fill='white')
    draw.text((54, 1012), '细节与方向检查', font=font(32, True), fill='white')
    for row in range(8):
        for col in range(16):
            x, y = 56 + col * 38, 1080 + row * 25
            draw.rectangle((x, y, x + 37, y + 24), fill='white' if (row + col) % 2 else '#253953')
    draw.text((54, 1330), '↑ 上方    左 ← → 右', font=font(34), fill='#67e8f9')
    draw.text((54, 1400), '由我们自己的 Windows 程序生成', font=font(28), fill='#94a3b8')
    return image


def fit_picture(path: str | Path) -> Image.Image:
    """按 EXIF 方向加载，保留比例；安全区属于这台屏的外壳配置。"""
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert('RGB')
        fitted = ImageOps.contain(source, (688, 1472), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', SIZE, BACKGROUND)
    canvas.paste(fitted, ((720 - fitted.width) // 2, 38 + (1472 - fitted.height) // 2))
    return canvas


def onboard_card() -> Image.Image:
    image = Image.new('RGB', SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((54, 100, 666, 1450), radius=30, outline='#31547a', width=3)
    draw.text((84, 175), 'ONBOARD', font=font(62, True), fill='#67e8f9')
    draw.text((84, 275), '板载媒体', font=font(54, True), fill='white')
    draw.rounded_rectangle((92, 420, 628, 930), radius=24, fill='#1e3046')
    draw.text((132, 500), 'T113', font=font(92, True), fill='white')
    draw.text((132, 635), '素材 → 副屏存储', font=font(36), fill='#cbd5e1')
    draw.text((132, 720), '由芯片自己解码播放', font=font(32), fill='#67e8f9')
    draw.text((84, 1040), '上传并激活后，电脑无需持续传帧', font=font(28), fill='#cbd5e1')
    draw.text((84, 1110), '当前素材槽＋上一版备份槽', font=font(26), fill='#94a3b8')
    draw.text((84, 1260), '校验通过才激活 · 失败不自动重试', font=font(24), fill='#94a3b8')
    return image


def desktop_card() -> Image.Image:
    image = Image.new('RGB', SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((54, 90, 666, 1460), radius=30, outline='#31547a', width=3)
    draw.text((82, 160), 'WINDOWS', font=font(64, True), fill='#67e8f9')
    draw.text((84, 255), '扩展桌面', font=font(52, True), fill='white')
    draw.rounded_rectangle((96, 390, 624, 910), radius=20, fill='#182a40', outline='#4b6485', width=3)
    draw.rectangle((130, 430, 590, 800), fill='#23465f')
    draw.rectangle((130, 430, 590, 485), fill='#32749b')
    draw.text((156, 528), '720 × 1568', font=font(48, True), fill='white')
    draw.text((156, 620), '桌面 → USB 副屏', font=font(34), fill='#cbd5e1')
    draw.text((84, 1010), '启动后捕获 Windows 扩展桌面', font=font(29), fill='#cbd5e1')
    draw.text((84, 1070), '此处为状态图，不是桌面镜像预览', font=font(25), fill='#94a3b8')
    draw.text((84, 1160), '最新帧优先 · 停止时完成当前整帧', font=font(25), fill='#94a3b8')
    draw.text((84, 1320), '请先确认虚拟显示器已正常工作', font=font(25), fill='#67e8f9')
    return image


def live_dashboard(metrics: dict, theme='青蓝', now: datetime | None = None) -> Image.Image:
    now = now or datetime.now()
    accent = '#67e8f9' if theme == '青蓝' else '#fbbf24'
    image = Image.new('RGB', SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((54, 70), 'OPEN SCREEN', font=font(46, True), fill=accent)
    draw.text((54, 145), '我的电脑 · 实时状态', font=font(34), fill='#94a3b8')
    draw.text((48, 255), now.strftime('%H:%M'), font=font(174, True), fill='white')
    draw.text((56, 488), now.strftime('%Y / %m / %d'), font=font(38), fill='#cbd5e1')
    draw.text((535, 483), now.strftime(':%S'), font=font(42, True), fill=accent)
    for label, key, y, caption in [
        ('CPU', 'cpu', 620, '处理器使用率'),
        ('RAM', 'ram', 960, f"{metrics['used_gb']:.1f} / {metrics['total_gb']:.1f} GB"),
    ]:
        value = max(0, min(100, metrics[key]))
        draw.rounded_rectangle((44, y, 675, y + 290), radius=28, fill='#1e3046')
        draw.text((76, y + 28), label, font=font(38, True), fill=accent)
        draw.text((76, y + 83), f'{value:.0f}%', font=font(82, True), fill='white')
        draw.text((330, y + 116), caption, font=font(24), fill='#94a3b8')
        draw.rounded_rectangle((78, y + 231, 640, y + 249), radius=9, fill='#34465e')
        if value > 0:
            draw.rounded_rectangle((78, y + 231, 78 + max(18, int(562 * value / 100)), y + 249),
                                   radius=9, fill=accent)
    disk = max(0, min(100, metrics.get('disk', 0)))
    draw.text((56, 1287), f"DISK  {disk:.0f}%  ·  {metrics.get('disk_used_gb', 0):.0f} / {metrics.get('disk_total_gb', 0):.0f} GB",
              font=font(27, True), fill='#cbd5e1')
    recv = metrics.get('net_recv_kib', 0) / 1024
    sent = metrics.get('net_sent_kib', 0) / 1024
    draw.text((56, 1340), f'NET  ↓ {recv:.2f} MB/s   ↑ {sent:.2f} MB/s',
              font=font(27, True), fill=accent)
    draw.ellipse((56, 1414, 72, 1430), fill='#22c55e')
    draw.text((90, 1402), 'WINDOWS 实时面板', font=font(28), fill='#94a3b8')
    draw.text((56, 1460), 'CPU · RAM · DISK · NETWORK', font=font(23), fill='#64748b')
    return image
