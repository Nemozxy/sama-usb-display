"""验收以成功发送跨度计时，不能用进程启动时间冒充一小时。"""
import pytest

import dashboard_health


@pytest.mark.parametrize('span,passed', [(3599.9, False), (3600, True)])
def test_one_hour_requires_entire_successful_frame_span(monkeypatch, span, passed):
    now = [0.]
    monkeypatch.setattr(dashboard_health.time, 'monotonic', lambda: now[0])
    health = dashboard_health.SessionHealth()
    health.connections = 1
    now[0] = 30.  # 握手/启动耗时不计入图像跨度。
    health.frame(.26)
    for i in range(1, 61):
        now[0] = 30 + span * i / 60
        health.frame(.26)
    report = health.report(mock=False, dropped=0, released=True, error=None)
    assert report['host_stability_1hour_passed'] == passed
    assert report['host_stability_30min_passed']
    assert not health.report(mock=True, dropped=0, released=True, error=None)['host_stability_1hour_passed']
    assert not health.report(mock=False, dropped=0, released=False, error=None)['host_stability_1hour_passed']
    health.offline_events = 1
    assert not health.report(mock=False, dropped=0, released=True, error=None)['host_stability_1hour_passed']


def test_one_hour_rejects_long_sending_gap(monkeypatch):
    now = [0.]
    monkeypatch.setattr(dashboard_health.time, 'monotonic', lambda: now[0])
    health = dashboard_health.SessionHealth()
    health.connections = 1
    for n in range(40):
        now[0] = n * 100
        health.frame(.26)
    assert not health.report(mock=False, dropped=0, released=True, error=None)['host_stability_1hour_passed']
