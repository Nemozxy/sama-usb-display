"""有界会话指标；百分位为 1ms 桶上界，不代表 LCD 光学延迟。"""
import math
import time

import psutil


class SessionHealth:
    def __init__(self):
        self.started = time.monotonic()
        self.process = psutil.Process()
        self.cpu_started = self._cpu()
        self.rss_started = self.process.memory_info().rss
        self.rss_max = self.rss_started
        self.last_memory = self.started
        self.frames = self.connections = self.offline_events = 0
        self.first_frame = self.last_frame = None
        self.max_gap = self.total_seconds = self.maximum = 0
        self.bins = [0] * 10001

    def _cpu(self):
        usage = self.process.cpu_times()
        return usage.user + usage.system

    def frame(self, seconds):
        now = time.monotonic()
        self.frames += 1
        self.total_seconds += seconds
        self.maximum = max(self.maximum, seconds)
        self.bins[min(10000, math.ceil(seconds * 1000))] += 1
        if self.first_frame is None:
            self.first_frame = now
        if self.last_frame is not None:
            self.max_gap = max(self.max_gap, now - self.last_frame)
        self.last_frame = now
        if now - self.last_memory >= 5:
            self.rss_max = max(self.rss_max, self.process.memory_info().rss)
            self.last_memory = now

    def percentile(self, fraction):
        if not self.frames:
            return None
        threshold = math.ceil(self.frames * fraction)
        count = 0
        for index, number in enumerate(self.bins):
            count += number
            if count >= threshold:
                return self.maximum if index == 10000 else index / 1000

    def report(self, *, mock, dropped, released, error):
        duration = max(.000001, time.monotonic() - self.started)
        span = (self.last_frame - self.first_frame) if self.frames > 1 else 0
        rss = self.process.memory_info().rss
        self.rss_max = max(rss, self.rss_max)
        stable = bool(not mock and self.offline_events == 0 and self.connections == 1
            and released and error is None and self.frames >= 30 and self.max_gap <= 65
            and self.last_frame is not None and time.monotonic() - self.last_frame <= 65)
        return dict(mock=mock,device_access=not mock,duration_seconds=duration,
            frames_sent=self.frames,frames_replaced_before_send=dropped,
            successful_connections=self.connections,reconnections=max(0,self.connections-1),
            offline_events=self.offline_events,port_released=released,fatal_error=error,
            first_to_last_frame_seconds=span,maximum_frame_gap_seconds=self.max_gap,
            average_fps_over_session=self.frames/duration,
            full_send_seconds=dict(mean=self.total_seconds/self.frames if self.frames else None,
                p95_upper=self.percentile(.95),p99_upper=self.percentile(.99),maximum=self.maximum),
            payload_bytes=self.frames*720*1568*4,wire_image_bytes=self.frames*4534250,
            payload_MB_per_session_second=self.frames*720*1568*4/duration/1e6,
            cpu_seconds=self._cpu()-self.cpu_started,
            cpu_percent_one_core=100*(self._cpu()-self.cpu_started)/duration,
            rss_start=self.rss_started,rss_end=rss,rss_sampled_max=self.rss_max,
            host_stability_30min_passed=bool(stable and span >= 1800),
            host_stability_1hour_passed=bool(stable and span >= 3600),
            visual_confirmation='pending',
            scope='full_send含主机编码/USB发送/有界RX；不是LCD延迟；RSS/CPU为本进程，FFmpeg子进程不在内')
