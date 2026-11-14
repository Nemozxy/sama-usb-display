from types import SimpleNamespace

import dashboard_live


def test_metric_sampler_reports_disk_and_network_rates(monkeypatch):
    networks = iter([SimpleNamespace(bytes_recv=1000, bytes_sent=500),
                     SimpleNamespace(bytes_recv=3048, bytes_sent=2548)])
    times = iter([10.0, 12.0])
    monkeypatch.setattr(dashboard_live.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(dashboard_live.psutil, 'cpu_percent', lambda interval: 12.5)
    monkeypatch.setattr(dashboard_live.psutil, 'virtual_memory',
                        lambda: SimpleNamespace(percent=40, used=4 * 1024**3, total=16 * 1024**3))
    monkeypatch.setattr(dashboard_live.psutil, 'disk_usage',
                        lambda path: SimpleNamespace(percent=50, used=100 * 1024**3, total=200 * 1024**3))
    monkeypatch.setattr(dashboard_live.psutil, 'net_io_counters', lambda: next(networks))
    sampler = dashboard_live.MetricSampler()
    first = sampler.sample()
    second = sampler.sample()
    assert first['net_recv_kib'] == first['net_sent_kib'] == 0
    assert second['cpu'] == 12.5 and second['ram'] == 40
    assert second['disk'] == 50 and second['disk_total_gb'] == 200
    assert second['net_recv_kib'] == second['net_sent_kib'] == 1


def test_network_counter_reset_does_not_produce_negative_rate(monkeypatch):
    networks = iter([SimpleNamespace(bytes_recv=1000, bytes_sent=1000),
                     SimpleNamespace(bytes_recv=10, bytes_sent=10)])
    times = iter([1.0, 2.0])
    monkeypatch.setattr(dashboard_live.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(dashboard_live.psutil, 'cpu_percent', lambda interval: 0)
    monkeypatch.setattr(dashboard_live.psutil, 'virtual_memory',
                        lambda: SimpleNamespace(percent=0, used=0, total=1))
    monkeypatch.setattr(dashboard_live.psutil, 'disk_usage',
                        lambda path: SimpleNamespace(percent=0, used=0, total=1))
    monkeypatch.setattr(dashboard_live.psutil, 'net_io_counters', lambda: next(networks))
    sampler = dashboard_live.MetricSampler()
    sampler.sample()
    result = sampler.sample()
    assert result['net_recv_kib'] == result['net_sent_kib'] == 0
