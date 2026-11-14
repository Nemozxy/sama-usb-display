"""真实 Windows 命名对象/跨进程通知，不创建窗口或访问副屏。"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from dashboard_instance import SingleInstance


@pytest.mark.skipif(os.name != 'nt', reason='Windows 命名对象')
def test_second_process_notifies_first_and_handles_release():
    name = 'Local\\SamaScreen.Test.' + uuid.uuid4().hex
    first = SingleInstance(name)
    try:
        assert first.primary and not first.take_show_request()
        script = ('from dashboard_instance import SingleInstance; '
            f'instance=SingleInstance({name!r}); '
            'assert not instance.primary; instance.close()')
        child = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=5,
            cwd=Path(__file__).resolve().parents[1])
        assert child.returncode == 0, child.stderr
        assert first.take_show_request() and not first.take_show_request()
    finally:
        first.close()
    replacement = SingleInstance(name)
    try:
        assert replacement.primary
    finally:
        replacement.close()
