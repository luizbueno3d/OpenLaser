"""A scripted fake JCZ/LMC board for driver tests. No USB is ever opened.

`FakeLmcBoard` stands in for galvoplotter's USB connection: it records every
12-byte single command and 3072-byte list packet, and answers status polls
from a small model of the board (READY = room for another list packet,
BUSY = executing). `fake_board()` wires it into `mopa_luiz.live` and blocks
pyusb and galvoplotter's USBConnection, so a test can never reach hardware.

`VirtualClock` lets the driver's waits advance time instead of sleeping, so a
20 s job or a 60 s frame loop runs in well under a second.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

import galvo.controller as gc
import usb.core
from galvo.consts import list_command_lookup, single_command_lookup

from mopa_luiz import live
from mopa_luiz.cli import MarkConfig


READY = 0x20
BUSY = 0x04
NOP = (0x8002, 0, 0, 0, 0, 0)

CFG = MarkConfig(
    path=Path("mem"),
    values={"FIELDSIZE": "200", "MINPWMFREQ": "1000", "MAXPWMFREQ": "4000000"},
)
BOX10 = [[[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0], [-5.0, -5.0]]]
DOT = [[[0.0, 0.0], [0.05, 0.0]]]
# Golden values from research report 05 (section 1.5 and Appendix A).
MARK_BOX10_LIST_SHA = "9ca1e12d5b53b6d56a4ceef65fedccb94c5255341a991529a8ef83d6ec0359f5"
DUMMY_PACKET_SHA = "e479558e2e4b35e14d00d3990477b8cb9646a94ca9502790fe12bb71c56b6a18"


def params(power=50.0, speed=1000.0, frequency_khz=30.0, pulse_width_ns=200.0) -> live.JobParams:
    return live.JobParams(
        text="",
        power=power,
        frequency_khz=frequency_khz,
        pulse_width_ns=pulse_width_ns,
        size_mm=10.0,
        mark_speed=speed,
    )


class VirtualClock:
    """Monotonic time that only moves when the driver waits (or a transfer happens)."""

    def __init__(self) -> None:
        self._t = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, seconds)

    def pause(self, event: threading.Event, seconds: float) -> bool:
        self.advance(seconds)
        return event.is_set()


class RealClock:
    def __init__(self) -> None:
        import time

        self._time = time

    def now(self) -> float:
        return self._time.monotonic()

    def advance(self, seconds: float) -> None:
        pass

    def pause(self, event: threading.Event, seconds: float) -> bool:
        return event.wait(seconds)


class FakeLmcBoard:
    """Model of the board's list buffer as seen through the status word.

    * Default: READY always set; BUSY for `busy_s` after ExecuteList.
    * `ready=False`: READY is never set (a board that never accepts a packet).
    * `capacity=N`: READY while fewer than N packets are queued. After
      ExecuteList the board executes one packet every `drain_s` seconds
      (`drain_s=None`: never, so READY sticks low once the buffer is full).
      BUSY while anything is queued.
    * `flicker_ready=True`: the READY bit toggles on every status read.
    * `read_failures=N`: every Nth status read fails (galvoplotter then
      reports (-1, -1, -1, -1)).
    """

    def __init__(
        self,
        clock=None,
        *,
        busy_s: float = 0.0,
        ready: bool = True,
        capacity: int | None = None,
        drain_s: float | None = None,
        transfer_s: float = 0.0002,
        flicker_ready: bool = False,
        read_failures: int = 0,
    ) -> None:
        self.clock = clock or RealClock()
        self.busy_s = busy_s
        self.ready = ready
        self.capacity = capacity
        self.drain_s = drain_s
        self.transfer_s = transfer_s
        self.flicker_ready = flicker_ready
        self.read_failures = read_failures
        self._reads = 0
        self.devices: dict[int, bool] = {}
        self.events: list[dict] = []
        self.violations: list[dict] = []
        self.exec_at: float | None = None
        self._queue: list[int] = []
        self._head_start: float | None = None
        self._last_single: int | None = None
        self._lock = threading.RLock()

    # -- board model -------------------------------------------------------
    def _drain(self, now: float) -> None:
        if self.capacity is None or self.exec_at is None or self.drain_s is None:
            return
        while self._queue and self._head_start is not None and now - self._head_start >= self.drain_s:
            self._queue.pop(0)
            self._head_start += self.drain_s
        if not self._queue:
            self._head_start = None

    def status(self) -> int:
        with self._lock:
            now = self.clock.now()
            self._drain(now)
            if not self.ready:
                return 0
            if self.capacity is not None:
                status = READY if len(self._queue) < self.capacity else 0
                if self.exec_at is not None and self._queue:
                    status |= BUSY
                return status
            busy = self.exec_at is not None and now - self.exec_at < self.busy_s
            ready = READY if not self.flicker_ready or self._reads % 2 else 0
            return ready | (BUSY if busy else 0)

    # -- galvoplotter connection interface ----------------------------------
    def is_open(self, index=0):
        return index in self.devices

    def open(self, index=0):
        self.devices[index] = True
        return index

    def close(self, index=0):
        self.devices.pop(index, None)
        self._record({"kind": "close"})

    def write(self, index=0, packet=None):
        data = bytes(packet)
        assert len(data) in (12, 0xC00), len(data)
        with self._lock:
            self.clock.advance(self.transfer_s)
            now = self.clock.now()
            if len(data) == 12:
                words = struct.unpack("<6H", data)
                self._last_single = words[0]
                if words[0] == 0x0005:  # ExecuteList
                    self.exec_at = now
                    if self._queue and self._head_start is None:
                        self._head_start = now
                elif words[0] in (0x001F, 0x0012):  # StopExecute, ResetList
                    self.exec_at = None
                    self._queue.clear()
                    self._head_start = None
                self._record({
                    "kind": "single",
                    "op": single_command_lookup.get(words[0], "?"),
                    "words": list(words),
                    "hex": data.hex(),
                })
                return
            ready_now = bool(self.status() & READY)
            cmds = [struct.unpack("<6H", data[i:i + 12]) for i in range(0, len(data), 12)]
            last = max((i for i, c in enumerate(cmds) if c != NOP), default=-1)
            event = {
                "kind": "list",
                "sha256": hashlib.sha256(data).hexdigest(),
                "n_real": last + 1,
                "cmds": [list(c) for c in cmds[: last + 1]],
                "ops": [list_command_lookup.get(c[0], "?") for c in cmds[: last + 1]],
                "ready_at_write": ready_now,
            }
            if not ready_now:
                self.violations.append(event)
            if self.capacity is not None:
                if self.exec_at is not None and not self._queue:
                    self._head_start = now
                self._queue.append(1)
            self._record(event)

    def read(self, index=0):
        status = self.status()
        with self._lock:
            self._reads += 1
            self.clock.advance(self.transfer_s)
            if self.read_failures and self._reads % self.read_failures == 0:
                self._record({"kind": "read", "after": "?", "status": None, "failed": True})
                raise ConnectionError("fake read failure")
            self._record({
                "kind": "read",
                "after": single_command_lookup.get(self._last_single, "?"),
                "status": status,
            })
        return struct.pack("<4H", 0, 0, 0, status)

    # -- helpers for assertions ----------------------------------------------
    def _record(self, event: dict) -> None:
        event["t"] = self.clock.now()
        self.events.append(event)

    def singles(self, op: str | None = None) -> list[dict]:
        return [e for e in self.events if e["kind"] == "single" and (op is None or e["op"] == op)]

    def lists(self) -> list[dict]:
        return [e for e in self.events if e["kind"] == "list"]

    def wire(self) -> list[dict]:
        """Every write in order (singles and list packets), without the status reads."""
        return [e for e in self.events if e["kind"] in ("single", "list")]


def _no_usb(*args, **kwargs):
    raise RuntimeError("USB access blocked by the fake-board test harness")


@contextmanager
def fake_board(board: FakeLmcBoard, *, scale: float = 1.1834, virtual_clock: VirtualClock | None = None):
    """Route mopa_luiz.live's controllers to `board`; real USB is unreachable."""
    with ExitStack() as stack:
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        calibration = tmp / "calibration.json"
        calibration.write_text(json.dumps({"scale_correction": scale}), encoding="utf-8")

        class FakeBoardController(gc.GalvoController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.connection = board

        stack.enter_context(mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "0"}))
        stack.enter_context(mock.patch.object(usb.core, "find", _no_usb))
        stack.enter_context(mock.patch.object(gc, "USBConnection", _no_usb))
        stack.enter_context(mock.patch.object(live, "CALIBRATION_PATH", calibration))
        stack.enter_context(mock.patch.object(live, "GalvoController", FakeBoardController))
        stack.enter_context(mock.patch.object(
            live, "ensure_libusb_backend", lambda: {"available": True, "source": "fake-board"}
        ))
        if hasattr(live, "WAIT_CLOCK"):
            stack.enter_context(mock.patch.object(live, "WAIT_CLOCK", virtual_clock or RealClock()))
        if hasattr(live, "BOARD_LOCK_PATH"):
            stack.enter_context(mock.patch.object(live, "BOARD_LOCK_PATH", tmp / "board.lock"))
        yield board
