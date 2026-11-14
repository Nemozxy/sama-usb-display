"""Host-side cmd 202/204 planning and bounded encode/send overlap."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

import differ_encoder as de
import image_encoder as ie
from idd_frame_bridge import LatestFrameMailbox

WIDTH, HEIGHT = 720, 1568
PIXEL_BYTES = WIDTH * HEIGHT * 4


@dataclass(frozen=True)
class UpdatePlan:
    sequence: int
    timestamp_ns: int
    kind: str
    data: bytes
    pixels: bytes
    command_sequence: int | None
    changed_pixels: int | None
    encode_seconds: float

    @property
    def wire_bytes(self):
        return len(self.data)

    def receipt(self, started, finished):
        return {
            "sequence": self.sequence,
            "timestamp_ns": self.timestamp_ns,
            "mode": self.kind,
            "command_sequence": self.command_sequence,
            "changed_pixels": self.changed_pixels,
            "wire_bytes": self.wire_bytes,
            "encode_ms": self.encode_seconds * 1000,
            "send_started_ns": started,
            "send_finished_ns": finished,
            "duration_ms": (finished - started) / 1e6,
            "pixel_sha256": hashlib.sha256(self.pixels).hexdigest(),
        }


class DifferPlanner:
    def __init__(self, *, change_threshold=0, reinit_interval=0):
        self.change_threshold = int(change_threshold)
        if not 0 <= self.change_threshold <= 255:
            raise ValueError("change threshold must be in 0..255")
        self.reinit_interval = int(reinit_interval)
        if self.reinit_interval < 0:
            raise ValueError("reinit interval must be nonnegative")
        self.previous = None
        self.needs_init = True
        self.command_sequence = 0

    def plan(self, pixels, *, sequence, timestamp_ns=0):
        pixels = bytes(pixels)
        if len(pixels) != PIXEL_BYTES:
            raise ValueError(f"expected {PIXEL_BYTES} BGRA bytes, got {len(pixels)}")
        started = time.perf_counter()
        initialize = (self.needs_init or
                      (self.reinit_interval and
                       self.command_sequence >= self.reinit_interval))
        kind = "initialize" if initialize else "delta"
        changed = None
        command_sequence = None
        try:
            if initialize:
                full = ie.plan_stream(ie.build_full_image(pixels, frames=False))
                data = full + full
            else:
                try:
                    if self.change_threshold:
                        delta, pixels = de.build_filtered_sequence_delta(
                            self.previous, pixels, self.change_threshold)
                    else:
                        delta = de.build_sequence_delta(self.previous, pixels)
                    changed = delta.changed_pixels
                    command_sequence = self.command_sequence
                    data = de.build_cmd204_delta_stream(
                        delta, sequence=command_sequence, allow_unverified=True)
                    full_size = 250 + ie.chunked_wire_size(len(pixels))
                    if len(data) >= full_size:
                        kind = "full"
                except ValueError as exc:
                    if "changed run exceeds" not in str(exc):
                        raise
                    kind = "full"
                if kind == "full":
                    data = ie.plan_stream(ie.build_full_image(pixels, frames=False))
                    command_sequence = None
        except Exception:
            raise
        elapsed = time.perf_counter() - started
        self.previous = pixels
        self.needs_init = kind == "full"
        if kind == "initialize":
            self.command_sequence = 0
        elif kind == "delta":
            self.command_sequence += 1
        return UpdatePlan(sequence, timestamp_ns, kind, data, pixels,
                          command_sequence, changed, elapsed)


class DifferPipeline:
    """Encode on the caller thread; write one completed plan on one worker."""

    def __init__(self, send, *, planner=None):
        self.planner = planner or DifferPlanner()
        self.send = send
        self.outbound = LatestFrameMailbox()
        self.error = None
        self.thread = None
        self.encoded_overwritten = 0

    def forward(self, mailbox, receipts, before_send=lambda: None):
        if self.thread is not None:
            raise RuntimeError("pipeline already ran")

        def write():
            try:
                while (plan := self.outbound.take()) is not None:
                    before_send()
                    started = time.perf_counter_ns()
                    self.send(plan)
                    finished = time.perf_counter_ns()
                    receipts.append(plan.receipt(started, finished))
            except BaseException as exc:
                self.error = exc
                self.outbound.abort()
                mailbox.abort()

        self.thread = threading.Thread(target=write, name="differ-writer")
        self.thread.start()
        try:
            while (frame := mailbox.take()) is not None:
                if self.error is not None:
                    raise self.error
                pixels = ie.encode_pixels(frame.image())
                plan = self.planner.plan(
                    pixels, sequence=frame.sequence, timestamp_ns=frame.timestamp_ns)
                self.outbound.put(plan)
        except BaseException:
            self.outbound.abort()
            raise
        finally:
            self.outbound.close()
            self.thread.join(15)
            if self.thread.is_alive():
                mailbox.abort()
                raise RuntimeError("differ writer did not stop within 15 seconds")
        if self.error is not None:
            raise self.error
        self.encoded_overwritten = self.outbound.overwritten
        return receipts
