"""Authorization gate for every device-writing operation.

Design rule: the tool is **read-only by default**, and every class of write has
its own independent switch.  Nothing that changes device state can happen by
accident, by a typo in a script, or by copy-pasting a query into a writer.

Typical use::

    from safety import Safety, WriteClass

    safety = Safety()                                  # read-only
    safety.authorize(WriteClass.IMAGE)                 # one class only
    assert safety.allows(WriteClass.IMAGE)             # True
    assert not safety.allows(WriteClass.REBOOT)        # still False

    safety.authorize_all()                             # explicit, noisy, refused
                                                       # without a reason string

Every authorization and every refused attempt is written to the audit log, so a
session can be reconstructed afterwards.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("sama.safety")


class WriteClass(enum.Enum):
    """Independent categories of state-changing operation.

    Each is authorized separately.  Authorizing one never implies another.
    """

    IMAGE = "image"          # opcode 101 / 102 -- push a full frame
    REGION = "region"        # opcode 251 -- factory region/position command
    INDEXED = "indexed"      # opcode 252 -- factory indexed buffered write
    UPLOAD = "upload"        # opcode 40 / 110 + 39 -- write a file to the device
    DELETE = "delete"        # opcode 42 -- delete a file or directory
    H264 = "h264"            # opcode 15 / 121 / 122 -- stream video
    LED = "led"              # per-segment LED payloads
    CONFIG = "config"        # opcode 125 -- writes PERSISTENT device configuration
    BRIGHTNESS = "brightness"  # opcode 14
    ROTATION = "rotation"    # opcode 13
    REBOOT = "reboot"        # opcode 11
    SHUTDOWN = "shutdown"    # opcode 12
    FACTORY = "factory"      # opcode 246 / 150 -- USB reset burst, test-step
    FIRMWARE = "firmware"    # UpdateRom() -- NEVER enabled by this tool


#: Classes this tool will never authorize, regardless of flags.
FORBIDDEN_FOREVER: frozenset[WriteClass] = frozenset({WriteClass.FIRMWARE})

#: Classes that change state on the device's persistent storage or configuration.
PERSISTENT: frozenset[WriteClass] = frozenset({
    WriteClass.CONFIG, WriteClass.UPLOAD, WriteClass.DELETE,
})

#: Classes that can leave the device unresponsive until re-plugged or rebooted.
DISRUPTIVE: frozenset[WriteClass] = frozenset({
    WriteClass.REBOOT, WriteClass.SHUTDOWN, WriteClass.FACTORY,
    WriteClass.REGION, WriteClass.INDEXED,
})


class WriteNotAuthorized(RuntimeError):
    """Raised when a write is attempted without its explicit authorization."""


class ForbiddenOperation(RuntimeError):
    """Raised for operations this tool refuses to perform at all."""


@dataclass
class AuditRecord:
    ts: float
    action: str
    detail: str
    allowed: bool


@dataclass
class Safety:
    """Tracks which write classes are authorized, and logs the decisions."""

    #: Human-readable reason for lifting read-only mode (required by authorize_all).
    reason: str = ""
    audit: list[AuditRecord] = field(default_factory=list)
    _allowed: set[WriteClass] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- state -------------------------------------------------------------

    @property
    def is_read_only(self) -> bool:
        with self._lock:
            return not self._allowed

    @property
    def allowed(self) -> frozenset[WriteClass]:
        with self._lock:
            return frozenset(self._allowed)

    def allows(self, wc: WriteClass) -> bool:
        if wc in FORBIDDEN_FOREVER:
            return False
        with self._lock:
            return wc in self._allowed

    # -- authorization -----------------------------------------------------

    def authorize(self, *classes: WriteClass, reason: str = "") -> None:
        """Enable one or more write classes.  FIRMWARE is always refused."""
        for wc in classes:
            if wc in FORBIDDEN_FOREVER:
                self._record("authorize", f"{wc.value} REFUSED (forbidden)", False)
                raise ForbiddenOperation(
                    f"{wc.value} is permanently disabled in this tool: firmware "
                    f"update / flashing is out of scope and cannot be enabled."
                )
            with self._lock:
                self._allowed.add(wc)
            self._record("authorize", wc.value, True)

    def authorize_all(self, reason: str) -> None:
        """Enable every non-forbidden class.  Requires a non-empty reason."""
        if not reason or not reason.strip():
            raise ValueError(
                "authorize_all() requires a written reason; it exists so that "
                "'enable everything' is never a silent accident."
            )
        self.reason = reason.strip()
        for wc in WriteClass:
            if wc in FORBIDDEN_FOREVER:
                continue
            with self._lock:
                self._allowed.add(wc)
        self._record("authorize_all", self.reason, True)

    def revoke_all(self) -> None:
        with self._lock:
            self._allowed.clear()
        self._record("revoke_all", "back to read-only", True)

    # -- enforcement -------------------------------------------------------

    def require(self, wc: WriteClass, detail: str = "") -> None:
        """Raise unless `wc` is authorized.  Call this immediately before a write."""
        if wc in FORBIDDEN_FOREVER:
            self._record("require", f"{wc.value} FORBIDDEN {detail}", False)
            raise ForbiddenOperation(f"{wc.value} is permanently disabled.")
        if not self.allows(wc):
            self._record("require", f"{wc.value} DENIED {detail}", False)
            raise WriteNotAuthorized(
                f"Write class '{wc.value}' is not authorized. This tool is "
                f"read-only by default."
                + (f"  ({detail})" if detail else "")
                + f"\nTo allow it, pass the corresponding flag, e.g. "
                  f"--allow {wc.value}."
                + ("\nNOTE: this class modifies persistent state on the device."
                   if wc in PERSISTENT else "")
                + ("\nNOTE: this class can leave the device unresponsive."
                   if wc in DISRUPTIVE else "")
            )
        self._record("require", f"{wc.value} ok {detail}", True)

    # -- audit -------------------------------------------------------------

    def _record(self, action: str, detail: str, allowed: bool) -> None:
        rec = AuditRecord(time.time(), action, detail, allowed)
        self.audit.append(rec)
        (log.info if allowed else log.warning)(
            "safety %-12s %s %s", action, "ALLOW" if allowed else "DENY", detail)

    def summary(self) -> str:
        if self.is_read_only:
            return ("READ-ONLY (no write class authorized). "
                    "Image/upload/delete/reboot/firmware are all disabled.")
        names = ", ".join(sorted(w.value for w in self.allowed))
        return f"writes enabled for: {names}" + (
            f"  [reason: {self.reason}]" if self.reason else "")

    def audit_lines(self) -> list[str]:
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(r.ts))} "
            f"{'ALLOW' if r.allowed else 'DENY '} {r.action:12s} {r.detail}"
            for r in self.audit
        ]
