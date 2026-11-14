"""Host-only settings validation and atomic replacement; no device imports."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import tempfile

DEFAULTS = {'interval': 1.0, 'theme': '青蓝', 'mode': '实时面板', 'picture': '', 'media': '',
            'pictures': [], 'desktop_broker': False, 'desktop_differ': False, 'desktop_fps': 15,
            'rotate_media': True, 'onboard_kind': 'video', 'onboard_fit': 'blur',
            'onboard_rotate': True, 'onboard_reboot': False, 'onboard_show': True,
            'onboard_target': 60}
CHOICES = {'theme': ('青蓝', '琥珀'),
           'mode': ('实时面板', '图片', 'GIF / 视频', '板载媒体', 'Windows 桌面', '测试画面'),
           'onboard_kind': ('video', 'image'), 'onboard_fit': ('pad', 'fill', 'blur')}


def normalize_settings(saved, *, strict=False):
    """Recover each field independently; strict mode rejects invalid saves."""
    if not isinstance(saved, Mapping):
        if strict:
            raise ValueError('设置须为对象')
        return dict(DEFAULTS)
    result = dict(DEFAULTS)
    for key in DEFAULTS:
        if key not in saved:
            continue
        value = saved[key]
        if key == 'interval':
            try:
                interval = float(value) if not isinstance(value, bool) else float('nan')
                valid = math.isfinite(interval) and 0.5 <= interval <= 60
            except (ValueError, TypeError, OverflowError):
                valid = False
            if valid:
                value = interval
        elif key in CHOICES:
            valid = isinstance(value, str) and value in CHOICES[key]
        elif key == 'pictures':
            valid = (isinstance(value, list) and len(value) <= 100 and
                     all(isinstance(path, str) and '\0' not in path for path in value))
            if valid:
                value = list(value)
        elif key in ('desktop_broker', 'desktop_differ', 'rotate_media',
                     'onboard_rotate', 'onboard_reboot', 'onboard_show'):
            valid = type(value) is bool
        elif key == 'desktop_fps':
            try:
                fps = int(value) if not isinstance(value, bool) else 0
                valid = str(fps) == str(value) and 1 <= fps <= 30
            except (ValueError, TypeError, OverflowError):
                valid = False
            if valid:
                value = fps
        elif key == 'onboard_target':
            try:
                target = int(value) if not isinstance(value, bool) else 0
                valid = str(target) == str(value) and 1 <= target <= 63
            except (ValueError, TypeError, OverflowError):
                valid = False
            if valid:
                value = target
        else:
            valid = isinstance(value, str) and '\0' not in value
        if valid:
            result[key] = value
        elif strict:
            raise ValueError(f'无效设置：{key}')
    return result


def load_settings(path):
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return normalize_settings(saved)


def save_settings(settings, path):
    # Validate/serialize before touching the destination directory or old file.
    payload = json.dumps(normalize_settings(settings, strict=True),
                         ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Same-directory exclusive temporary prevents unrelated .tmp clobbering
        # and permits same-filesystem replace. Concurrent successful saves are
        # last-writer wins; Windows sharing failures propagate without retry or
        # deletion of the old config. Close before replace on Windows.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent,
                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
