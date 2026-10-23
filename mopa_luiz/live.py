from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import signal
import struct
import sys
import threading
import time
from typing import Any, Callable

from galvo.consts import (
    ExecuteList,
    ResetList,
    listDelayTime,
    listJumpDelay,
    listJumpSpeed,
    listJumpTo,
    listLaserOffDelay,
    listLaserOnDelay,
    listLaserOnPoint,
    listMarkSpeed,
    listMarkTo,
    listPolygonDelay,
    list_command_lookup,
    single_command_lookup,
)
from galvo.controller import GalvoController

from .usb_backend import ensure_libusb_backend

from .cli import (
    MarkConfig,
    build_test_box_plan,
    nearest_pulse_width,
    pulse_width_table,
)
from .safety import evaluate_emission


HARDWARE_JOB_TIMEOUT_S = 60.0
ACTIVE_HARDWARE_PROCESSES: dict[int, mp.Process] = {}
ACTIVE_HARDWARE_LOCK = threading.Lock()
STOP_REQUESTED = threading.Event()

# Killing a hardware job mid-USB-transfer can leave the LMC board waiting for
# the rest of a packet forever ("wedged": it connects but never replies, until
# the machine is power-cycled). So job children abort the board cleanly on
# SIGTERM, and connects run under a hard deadline with a wedge diagnosis.
_ACTIVE_CONTROLLER: GalvoController | None = None
GRACEFUL_STOP_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 12.0

CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "calibration.json"
# A galvo field calibration is a small trim, so clamp hard: a fat-fingered
# value must never wildly mis-size a laser job.
SCALE_CORRECTION_MIN = 0.5
SCALE_CORRECTION_MAX = 2.0


def mock_mode() -> bool:
    """OPENMOPA_MOCK=1 exercises the full command chain on a mock connection."""
    return os.environ.get("OPENMOPA_MOCK", "") == "1"


def load_scale_correction() -> float:
    """Persisted mark-size correction (measured / intended). 1.0 = no correction."""
    try:
        with CALIBRATION_PATH.open("r", encoding="utf-8") as fh:
            value = float(json.load(fh).get("scale_correction", 1.0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1.0
    if not value > 0:
        return 1.0
    return max(SCALE_CORRECTION_MIN, min(SCALE_CORRECTION_MAX, value))


def save_scale_correction(value: object) -> float:
    """Persist the mark-size correction factor, rejecting out-of-band values."""
    number = float(value)  # raises TypeError/ValueError for None/junk; caller reports it
    if not SCALE_CORRECTION_MIN <= number <= SCALE_CORRECTION_MAX:
        raise ValueError(
            f"scale_correction must be between {SCALE_CORRECTION_MIN} and {SCALE_CORRECTION_MAX}"
        )
    CALIBRATION_PATH.write_text(
        json.dumps({"scale_correction": number}, indent=2) + "\n", encoding="utf-8"
    )
    return number


def effective_field_size(cfg: MarkConfig) -> float:
    """Field size used for every mm<->galvo conversion: nominal x calibration."""
    base = cfg.get_float("FIELDSIZE", 200.0) or 200.0
    return base * load_scale_correction()

FONT_5X7 = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


@dataclass(frozen=True)
class JobParams:
    text: str
    power: float
    frequency_khz: float
    pulse_width_ns: float
    size_mm: float
    mark_speed: float


def stop_controller(controller: GalvoController) -> None:
    """Release any pending board waits, then abort and disconnect."""
    stop_event = getattr(controller, "_openlaser_stop", None)
    if stop_event is not None:
        stop_event.set()
    controller.shutdown()


def _graceful_hardware_stop() -> None:
    """Abort the board cleanly (stop execution, reset list, close MO). Never raises."""
    controller = _ACTIVE_CONTROLLER
    if controller is None:
        return
    # shutdown() can block if the board is mute; a watchdog guarantees exit.
    watchdog = threading.Timer(GRACEFUL_STOP_TIMEOUT_S, lambda: os._exit(137))
    watchdog.daemon = True
    watchdog.start()
    try:
        stop_controller(controller)
    except Exception:
        pass
    finally:
        watchdog.cancel()


def _sigterm_abort_handler(signum, frame) -> None:
    try:
        _graceful_hardware_stop()
    finally:
        os._exit(143)


def _hardware_job_entry(conn, function_name: str, args: tuple, kwargs: dict) -> None:
    signal.signal(signal.SIGTERM, _sigterm_abort_handler)
    try:
        result = globals()[function_name](*args, **kwargs)
        conn.send({"ok": True, "result": result})
    except BaseException as exc:
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


def run_hardware_job(function_name: str, *args, timeout_s: float = HARDWARE_JOB_TIMEOUT_S, **kwargs) -> dict[str, Any]:
    if STOP_REQUESTED.is_set():
        raise RuntimeError("hardware stop requested")
    STOP_REQUESTED.clear()
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_hardware_job_entry,
        args=(child_conn, function_name, args, kwargs),
    )
    process.start()
    with ACTIVE_HARDWARE_LOCK:
        ACTIVE_HARDWARE_PROCESSES[process.pid or id(process)] = process
    child_conn.close()
    message = None
    try:
        if parent_conn.poll(timeout_s):
            message = parent_conn.recv()
        else:
            # SIGTERM lets the child abort the board cleanly before exiting;
            # a hard kill mid-USB-transfer wedges the controller firmware.
            process.terminate()
            process.join(GRACEFUL_STOP_TIMEOUT_S + 2.0)
            if process.is_alive():
                process.kill()
                process.join(3.0)
            raise TimeoutError(
                f"hardware job {function_name} timed out after {timeout_s:.0f}s. "
                "If the laser stopped responding, power-cycle the machine before retrying."
            )
    finally:
        parent_conn.close()
        with ACTIVE_HARDWARE_LOCK:
            ACTIVE_HARDWARE_PROCESSES.pop(process.pid or id(process), None)
    process.join(3.0)
    if message is None:
        raise RuntimeError(f"hardware job {function_name} ended without a result")
    if not message.get("ok"):
        raise RuntimeError(str(message.get("error") or f"hardware job {function_name} failed"))
    if process.exitcode not in (0, None):
        raise RuntimeError(f"hardware job {function_name} exited with code {process.exitcode}")
    return message["result"]


def stop_active_hardware_jobs() -> dict[str, Any]:
    STOP_REQUESTED.set()
    with ACTIVE_HARDWARE_LOCK:
        processes = list(ACTIVE_HARDWARE_PROCESSES.values())
    stopped = 0
    for process in processes:
        if process.is_alive():
            process.terminate()
            stopped += 1
    for process in processes:
        # Give the SIGTERM handler time to abort the board cleanly (its own
        # watchdog guarantees exit within GRACEFUL_STOP_TIMEOUT_S).
        process.join(GRACEFUL_STOP_TIMEOUT_S + 2.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
    with ACTIVE_HARDWARE_LOCK:
        ACTIVE_HARDWARE_PROCESSES.clear()
    return {"ok": True, "operation": "stop_hardware_jobs", "stopped_processes": stopped}


def clear_stop_request() -> None:
    STOP_REQUESTED.clear()


def stop_requested() -> bool:
    return STOP_REQUESTED.is_set()


def mm_to_galvo(x_mm: float, y_mm: float, field_size_mm: float) -> tuple[int, int]:
    scale = 0xFFFF / field_size_mm
    x = int(round(0x8000 + x_mm * scale))
    y = int(round(0x8000 + y_mm * scale))
    return max(0, min(0xFFFF, x)), max(0, min(0xFFFF, y))


MAX_JOB_PATHS = 3000
MAX_PATH_POINTS = 2000
MAX_JOB_POINTS = 20000
# Float noise from transforms must not reject a shape drawn exactly on the edge.
FIELD_EDGE_TOLERANCE_MM = 1e-6


def geometry_errors(polylines: list[list[list[float]]], field_size_mm: float) -> list[str]:
    """Reasons this geometry cannot be sent to the galvo as-is; empty when OK.

    The galvo must burn exactly what was planned, so out-of-field points and
    oversized jobs are refused rather than clamped or truncated.
    """
    errors: list[str] = []
    limit = field_size_mm / 2.0 + FIELD_EDGE_TOLERANCE_MM
    if len(polylines) > MAX_JOB_PATHS:
        errors.append(
            f"job has {len(polylines)} paths; the limit per layer pass is {MAX_JOB_PATHS}. "
            "Split the job, or use a larger raster pitch."
        )
    total_points = 0
    outside = 0
    first_outside: tuple[float, float] | None = None
    longest = 0
    for polyline in polylines:
        longest = max(longest, len(polyline))
        for raw in polyline:
            if len(raw) < 2:
                continue
            total_points += 1
            x, y = float(raw[0]), float(raw[1])
            if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > limit or abs(y) > limit:
                outside += 1
                if first_outside is None:
                    first_outside = (x, y)
    if longest > MAX_PATH_POINTS:
        errors.append(f"a path has {longest} points; the limit is {MAX_PATH_POINTS} per path.")
    if total_points > MAX_JOB_POINTS:
        errors.append(
            f"job has {total_points} points; the limit per layer pass is {MAX_JOB_POINTS}. "
            "Split the job, or simplify the artwork."
        )
    if first_outside is not None:
        errors.append(
            f"{outside} point(s) are outside the {field_size_mm:.2f} mm galvo field "
            f"(first at {first_outside[0]:.2f}, {first_outside[1]:.2f} mm). "
            "Move or scale the artwork inside the field."
        )
    return errors


FRAME_LIGHT_SPEED_MM_S = 3000.0
# One pass of a small shape at light speed lasts a few milliseconds, too short
# to see, so Frame Once retraces it for roughly this long.
FRAME_ONCE_SECONDS = 2.0


def frame_once_repeats(polylines: list[list[list[float]]], point_count: int) -> int:
    length_mm = 0.0
    previous: tuple[float, float] | None = None
    for polyline in polylines:
        for raw in polyline:
            point = (float(raw[0]), float(raw[1]))
            if previous is not None:
                length_mm += math.dist(previous, point)
            previous = point
    if previous is not None and polylines and polylines[0]:
        length_mm += math.dist(previous, (float(polylines[0][0][0]), float(polylines[0][0][1])))
    pass_seconds = max(length_mm / FRAME_LIGHT_SPEED_MM_S, 1e-3)
    by_time = math.ceil(FRAME_ONCE_SECONDS / pass_seconds)
    by_size = max(1, MAX_JOB_POINTS // max(1, point_count))
    return max(1, min(by_time, by_size, 500))


def sanitize_polylines(polylines: list[list[list[float]]], field_size_mm: float) -> list[list[tuple[int, int]]]:
    errors = geometry_errors(polylines, field_size_mm)
    if errors:
        raise ValueError(" ".join(errors))
    clean: list[list[tuple[int, int]]] = []
    for polyline in polylines:
        points = [mm_to_galvo(float(raw[0]), float(raw[1]), field_size_mm) for raw in polyline if len(raw) >= 2]
        if len(points) >= 2:
            clean.append(points)
    if not clean:
        raise ValueError("no markable geometry in job")
    return clean


def box_points(size_mm: float, field_size_mm: float) -> list[tuple[int, int]]:
    half = size_mm / 2.0
    return [
        mm_to_galvo(-half, -half, field_size_mm),
        mm_to_galvo(half, -half, field_size_mm),
        mm_to_galvo(half, half, field_size_mm),
        mm_to_galvo(-half, half, field_size_mm),
        mm_to_galvo(-half, -half, field_size_mm),
    ]


def text_segments(text: str, size_mm: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    text = "".join(ch for ch in text.upper() if ch in FONT_5X7)[:18]
    if not text.strip():
        return []
    text_height = max(1.0, size_mm * 0.36)
    cell = text_height / 7.0
    total_width = len(text) * 6 * cell - cell
    x0 = -total_width / 2.0
    y0 = text_height / 2.0
    segments = []
    for index, char in enumerate(text):
        glyph = FONT_5X7[char]
        char_x = x0 + index * 6 * cell
        for row, bits in enumerate(glyph):
            y = y0 - row * cell
            col = 0
            while col < 5:
                if bits[col] == "0":
                    col += 1
                    continue
                start = col
                while col < 5 and bits[col] == "1":
                    col += 1
                x_start = char_x + start * cell
                x_end = char_x + col * cell - cell * 0.2
                segments.append(((x_start, y), (x_end, y)))
    return segments


def connect_controller(
    cfg: MarkConfig, params: JobParams, connect_timeout_s: float = CONNECT_TIMEOUT_S
) -> GalvoController:
    # Hardware jobs run in fresh spawned processes, so load libusb here too.
    ensure_libusb_backend()
    field_size = effective_field_size(cfg)
    usb_events: list[str] = []
    trace = UsbTrace.from_env()

    def usb_log(message: str) -> None:
        usb_events.append(message)
        if trace is not None:
            trace.event("LOG", message=str(message))

    controller = GalvoController(
        power=params.power,
        frequency=params.frequency_khz,
        pulse_width=nearest_pulse_width(params.pulse_width_ns, pulse_width_table(cfg)),
        mark_speed=params.mark_speed,
        travel_speed=3000.0,
        light_speed=FRAME_LIGHT_SPEED_MM_S,
        galvos_per_mm=0xFFFF / field_size,
        mock=mock_mode(),
        usb_log=usb_log,
    )
    apply_wait_timeouts(controller, trace=trace)
    connect_errors: list[BaseException] = []

    def _connect() -> None:
        try:
            controller.connect_if_needed()
        except BaseException as exc:  # surfaced below with the usb_log tail
            connect_errors.append(exc)

    # A wedged board can keep galvoplotter's retry loop spinning far past its
    # own attempt limit (its USB resets knock the device off the bus
    # mid-retry), so enforce a hard deadline of our own.
    worker = threading.Thread(target=_connect, daemon=True)
    worker.start()
    worker.join(connect_timeout_s)
    if worker.is_alive() or connect_errors:
        controller.abort_connect()
        controller._sending = False  # make shutdown() safe: no further USB writes
        try:
            controller.shutdown()
        except Exception:
            pass
        cause = connect_errors[0] if connect_errors else None
        detail = "; ".join(usb_events[-4:]) or (str(cause) if cause else "connect timed out")
        log_text = " ".join(usb_events + [str(cause or "")]).lower()
        if any("claim: Success" in event for event in usb_events):
            hint = (
                "The board connects but is not responding (wedged controller). "
                "Power-cycle the laser (full power off, wait 15s, power on), then retry."
            )
        elif sys.platform.startswith("win") and any(
            marker in log_text for marker in ("not supported", "not implemented", "entity not found", "errno 40")
        ):
            hint = (
                "Windows is using the EZCAD (BJJCZ) driver for the laser. Install the WinUSB "
                "driver for USBLMCV4 with Zadig (see the README), then retry."
            )
        elif any(marker in log_text for marker in ("access denied", "errno 13", "permission")):
            hint = (
                "This user is not allowed to open the laser's USB device. On Linux, install "
                "scripts/99-openlaser.rules (see the README), then unplug and replug the laser."
            )
        else:
            hint = "Check power, USB cable, and the Detect panel, then retry."
        raise RuntimeError(
            f"cannot reach the JCZ/LMC laser over USB. {hint} Log: {detail}"
        ) from cause
    global _ACTIVE_CONTROLLER
    _ACTIVE_CONTROLLER = controller
    return controller


STATUS_READY = 0x20  # the board can take another list packet
STATUS_BUSY = 0x04  # the board is executing a list
LIST_PACKET_BYTES = 0xC00
# Before ExecuteList nothing drains, so a board that withholds READY is stuck.
PRE_EXECUTE_WAIT_S = 2.0
WAIT_BUDGET_FACTOR = 1.5
WAIT_BUDGET_SLACK_S = 2.0
WAIT_POLL_S = 0.01
# galvoplotter documents the laser on/off, polygon and jump delays in us, but
# MeerK40t sends delay/10, i.e. 10 us units (hardware item H9). Estimates use
# the longer reading: a generous budget only delays a stop, a tight one aborts
# good jobs. DelayTime and LaserOnPoint are documented in 10 us units.
LIST_DELAY_UNIT_S = 10e-6
LIST_COMMAND_OVERHEAD_S = 10e-6
FRAME_JUMP_DELAY = 8  # galvoplotter's delay_jump_short
MARK_DELAYS = (100, 100, 100)  # galvoplotter's laser on, laser off, polygon delays
MO_OPEN_SETTLE = 800  # listDelayTime(delay_open_mo * 100), 10 us units


class BoardWaitTimeout(RuntimeError):
    """The board made no progress within its wait budget; the job was stopped."""


class _RealWaitClock:
    def now(self) -> float:
        return time.monotonic()

    def pause(self, event: threading.Event, seconds: float) -> bool:
        return event.wait(seconds)


# Tests swap this for a virtual clock so long simulated jobs run instantly.
WAIT_CLOCK: Any = _RealWaitClock()


class ListTimeEstimator:
    """How long the board takes to execute each list command, in seconds.

    Follows the speed and delay settings as they appear in the list. Every
    doubt resolves towards a longer estimate.
    """

    def __init__(self, galvos_per_mm: float, jump_mm_s: float, mark_mm_s: float) -> None:
        self._fallback_jump_units = max(1, int(jump_mm_s * galvos_per_mm / 1000.0))
        self._fallback_mark_units = max(1, int(mark_mm_s * galvos_per_mm / 1000.0))
        self.reset()

    def reset(self) -> None:
        self.jump_units: int | None = None
        self.mark_units: int | None = None
        self.jump_delay_s = 0.0
        self.laser_delays_s = [0.0, 0.0, 0.0]  # on, off, polygon
        self.x = self.y = 0x8000

    def command_seconds(self, command: int, v1=0, v2=0, v3=0, v4=0, v5=0) -> float:
        seconds = LIST_COMMAND_OVERHEAD_S
        if command == listJumpSpeed:
            self.jump_units = int(v1)
        elif command == listMarkSpeed:
            self.mark_units = int(v1)
        elif command == listJumpDelay:
            self.jump_delay_s = abs(int(v1)) * LIST_DELAY_UNIT_S
        elif command == listLaserOnDelay:
            self.laser_delays_s[0] = abs(int(v1)) * LIST_DELAY_UNIT_S
        elif command == listLaserOffDelay:
            self.laser_delays_s[1] = abs(int(v1)) * LIST_DELAY_UNIT_S
        elif command == listPolygonDelay:
            self.laser_delays_s[2] = abs(int(v1)) * LIST_DELAY_UNIT_S
        elif command in (listDelayTime, listLaserOnPoint):
            seconds += abs(int(v1)) * 10e-6
        elif command in (listJumpTo, listMarkTo):
            x, y = int(v1), int(v2)
            distance = max(float(v4), math.hypot(x - self.x, y - self.y))
            self.x, self.y = x, y
            if command == listJumpTo:
                units = self._fallback_jump_units if self.jump_units is None else self.jump_units
                seconds += distance / max(1, units) / 1000.0 + self.jump_delay_s
            else:
                units = self._fallback_mark_units if self.mark_units is None else self.mark_units
                seconds += distance / max(1, units) / 1000.0 + sum(self.laser_delays_s)
        return seconds


class UsbTrace:
    """Opt-in timestamped trace of every USB transfer: OPENMOPA_USB_TRACE=<file>.

    One JSON object per line. Hardware sessions use it to check, for example,
    that Fiber_SetMo(0) comes only after BUSY clears.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "UsbTrace | None":
        raw = os.environ.get("OPENMOPA_USB_TRACE", "").strip()
        if not raw:
            return None
        try:
            return cls(Path(raw).expanduser())
        except OSError:
            return None

    def event(self, direction: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="microseconds"),
            "mono": round(time.monotonic(), 6),
            "pid": os.getpid(),
            "dir": direction,
            **fields,
        }
        with self._lock:
            if not self._fh.closed:
                self._fh.write(json.dumps(record) + "\n")

    def write(self, data: bytes) -> None:
        if len(data) == 12:
            words = struct.unpack("<6H", data)
            self.event("W", op=single_command_lookup.get(words[0], f"0x{words[0]:04x}"), words=list(words))
            return
        cmds = [struct.unpack("<6H", data[i:i + 12]) for i in range(0, len(data), 12)]
        last = max((i for i, c in enumerate(cmds) if c != (0x8002, 0, 0, 0, 0, 0)), default=-1)
        self.event(
            "W",
            op="list",
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            cmds=[[list_command_lookup.get(c[0], f"0x{c[0]:04x}"), *c[1:]] for c in cmds[: last + 1]],
        )

    def read(self, reply: Any) -> None:
        words = list(reply) if reply is not None else None
        status = words[3] if words and len(words) == 4 else None
        self.event("R", words=words, status=None if status is None or status < 0 else f"0x{status:04x}")

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class BoardWaits:
    """Per-wait, progress-aware deadlines for one controller.

    Each list packet's execution time is estimated as it is built. A wait
    for READY / not BUSY gets its own clock, restarted whenever the status
    word changes, with a budget of
        1.5 x max(projected end - now, largest of the last 3 packet estimates) + 2 s
    where the projected end is the ExecuteList time plus the estimates of
    every packet written so far. Before ExecuteList nothing drains, so the
    budget is 2 s. A wait never proceeds on expiry: it latches a fault and
    stops the job, and no list packet is written after that.
    """

    def __init__(self, controller: GalvoController, clock: Any, trace: UsbTrace | None = None) -> None:
        self.controller = controller
        self.clock = clock
        self.trace = trace
        self.stop = threading.Event()
        self.fault: str | None = None
        self.fault_at: float | None = None
        galvos_per_mm = abs(float(getattr(controller, "galvos_per_mm", 0xFFFF / 200.0)))
        self.estimator = ListTimeEstimator(
            galvos_per_mm,
            jump_mm_s=float(getattr(controller, "travel_speed", 0) or 3000.0),
            mark_mm_s=float(getattr(controller, "mark_speed", 0) or 100.0),
        )
        self.packets: list[float] = []
        self.pending_s = 0.0
        self.execute_at: float | None = None
        self.aborting = False
        self.last_budget_s: float | None = None
        self.shut_down = False
        self._shutdown_lock = threading.Lock()

    # -- list bookkeeping ----------------------------------------------------
    def reset_list(self) -> None:
        self.packets = []
        self.pending_s = 0.0
        self.execute_at = None
        self.estimator.reset()

    def projected_end(self) -> float | None:
        if self.execute_at is None:
            return None
        return self.execute_at + sum(self.packets)

    def budget(self, now: float) -> float:
        end = self.projected_end()
        if end is None:
            return PRE_EXECUTE_WAIT_S
        recent = max(self.packets[-3:], default=0.0)
        return WAIT_BUDGET_FACTOR * max(end - now, recent) + WAIT_BUDGET_SLACK_S

    def on_spooler(self) -> bool:
        return threading.current_thread() is self.controller._spooler_thread

    def record_fault(self, reason: str) -> str:
        if self.fault is None:
            self.fault = reason
            self.fault_at = self.clock.now()
            if self.trace is not None:
                self.trace.event("FAULT", reason=reason)
        self.stop.set()
        return self.fault

    # -- the waits -----------------------------------------------------------
    def wait(self, satisfied: Callable[[int], bool], label: str) -> None:
        if self.stop.is_set():
            return self._stopped()
        controller = self.controller
        last_status: int | None = None
        deadline = budget = 0.0
        while True:
            if not controller._sending:
                return
            status = controller.status()
            # A failed read comes back as -1 (all bits set): never "satisfied".
            if status >= 0 and satisfied(status):
                return
            now = self.clock.now()
            if status != last_status:
                last_status = status
                budget = self.budget(now)
                deadline = now + budget
                self.last_budget_s = budget
            elif now >= deadline:
                word = "no reply" if status < 0 else f"status 0x{status:04x}"
                fault = self.record_fault(
                    f"board not {label} within {budget:.1f} s ({word}, no change). "
                    "The job was stopped; nothing more was sent. Check the laser, then retry."
                )
                if self.on_spooler():
                    raise ConnectionAbortedError(fault)
                raise BoardWaitTimeout(fault)
            if self.clock.pause(self.stop, WAIT_POLL_S):
                return self._stopped()

    def _stopped(self) -> None:
        # The spooler sits in _list_end holding the list-build lock, about to
        # push its next packet. On stop it must drop that packet: writing into
        # a full list buffer times out, the driver then resets the USB device
        # on every following command, and a stop took 3-6 s and risked wedging
        # the board. galvoplotter's spooler exits cleanly on this error.
        if self.on_spooler():
            raise ConnectionAbortedError(self.fault or "stop requested")
        return None

    def wait_ready(self) -> None:
        self.wait(lambda s: bool(s & STATUS_READY), "READY")

    def wait_idle(self) -> None:
        self.wait(lambda s: not s & STATUS_BUSY, "idle")

    def wait_finished(self) -> None:
        self.wait(lambda s: bool(s & STATUS_READY) and not s & STATUS_BUSY, "READY and idle")

    # -- hooks around galvoplotter ----------------------------------------------
    def wrap(self, original_list_write: Callable, original_send: Callable, original_shutdown: Callable) -> None:
        def list_write(command, v1=0, v2=0, v3=0, v4=0, v5=0):
            seconds = self.estimator.command_seconds(command, v1, v2, v3, v4, v5)
            # May flush the previous packet first; this command lands in the next one.
            original_list_write(command, v1, v2, v3, v4, v5)
            self.pending_s += seconds

        def send(data, read=True):
            if len(data) == LIST_PACKET_BYTES:
                if self.fault is not None and not self.aborting:
                    # Never write a list packet after a watchdog expiry.
                    if self.on_spooler():
                        raise ConnectionAbortedError(self.fault)
                    return None
                self.packets.append(self.pending_s)
                self.pending_s = 0.0
            elif len(data) == 12:
                opcode = data[0] | (data[1] << 8)
                if opcode == ResetList:
                    self.reset_list()
                elif opcode == ExecuteList and self.execute_at is None:
                    self.execute_at = self.clock.now()
            traced = self.trace is not None and self.controller._sending
            if traced:
                self.trace.write(bytes(data))
            reply = original_send(data, read)
            if traced and read:
                self.trace.read(reply)
            return reply

        def shutdown(*args, **kwargs):
            with self._shutdown_lock:
                if self.shut_down:
                    return
                self.shut_down = True
            try:
                original_shutdown(*args, **kwargs)
            finally:
                # galvoplotter's send() reconnects (and re-inits the board) if
                # anything writes after disconnect. Nothing may.
                self.controller._sending = False
                if self.trace is not None:
                    self.trace.close()

        self.controller._list_write = list_write  # type: ignore[method-assign]
        self.controller.send = send  # type: ignore[method-assign]
        self.controller.shutdown = shutdown  # type: ignore[method-assign]


def apply_wait_timeouts(controller: GalvoController, *, clock: Any = None, trace: UsbTrace | None = None) -> BoardWaits:
    """Replace galvoplotter's unbounded waits with per-wait, progress-aware deadlines."""
    waits = BoardWaits(controller, clock or WAIT_CLOCK, trace)
    controller._openlaser_waits = waits  # type: ignore[attr-defined]
    controller._openlaser_stop = waits.stop  # type: ignore[attr-defined]
    controller.wait_ready = waits.wait_ready  # type: ignore[method-assign]
    controller.wait_idle = waits.wait_idle  # type: ignore[method-assign]
    controller.wait_finished = waits.wait_finished  # type: ignore[method-assign]
    waits.wrap(controller._list_write, controller.send, controller.shutdown)
    return waits


def _guard_job(controller: GalvoController, job: Callable, on_fault: Callable[[str], None] | None = None) -> Callable:
    """Wrap a spooler job so a stop or fault always releases the main thread.

    galvoplotter's _spooler_run returns on ConnectionAbortedError without
    removing the job, so wait_for_spooler_send would wait forever. Clearing
    the queue here, after the list-build lock is released, keeps lock order.
    """
    waits: BoardWaits = controller._openlaser_waits  # type: ignore[attr-defined]

    def guarded(c: GalvoController) -> bool:
        try:
            return job(c)
        except BaseException as exc:
            if not isinstance(exc, ConnectionAbortedError):
                waits.record_fault(f"hardware job failed: {type(exc).__name__}: {exc}")
            with c._spooler_lock:
                c._queue.clear()
                c._spooler_lock.notify_all()
            if waits.fault is not None and on_fault is not None:
                on_fault(waits.fault)
            if isinstance(exc, ConnectionAbortedError):
                raise
            raise ConnectionAbortedError(waits.fault) from exc

    return guarded


def _run_job(controller: GalvoController, job: Callable) -> None:
    """Submit a one-shot list job, wait until the board finishes it, surface faults."""
    waits: BoardWaits = controller._openlaser_waits  # type: ignore[attr-defined]
    controller.submit(_guard_job(controller, job))
    with controller._spooler_lock:
        while controller._queue:
            thread = controller._spooler_thread
            if thread is not None and not thread.is_alive():
                waits.record_fault("the galvo spooler stopped before the job was sent")
                break
            controller._spooler_lock.wait(0.1)
    if waits.fault is None:
        controller.wait_finished()
    if waits.fault is not None:
        raise BoardWaitTimeout(waits.fault)
    if waits.stop.is_set():
        raise RuntimeError("hardware stop requested before the board finished the job")


def hardware_job_timeout_s(estimate_s: float) -> float:
    """Outer deadline for a spawned hardware job: max(60 s, 2 x estimate + 60 s)."""
    return max(HARDWARE_JOB_TIMEOUT_S, 2.0 * max(0.0, estimate_s) + HARDWARE_JOB_TIMEOUT_S)


def _paths_seconds(
    polylines: list[list[list[float]]], galvos_per_mm: float, jump_mm_s: float, draw_mm_s: float, mark: bool
) -> float:
    timer = ListTimeEstimator(galvos_per_mm, jump_mm_s, draw_mm_s)
    timer.command_seconds(listJumpSpeed, int(jump_mm_s * galvos_per_mm / 1000.0))
    timer.command_seconds(listJumpDelay, FRAME_JUMP_DELAY)
    if mark:
        timer.command_seconds(listMarkSpeed, int(draw_mm_s * galvos_per_mm / 1000.0))
        for command, delay in zip((listLaserOnDelay, listLaserOffDelay, listPolygonDelay), MARK_DELAYS):
            timer.command_seconds(command, delay)
    seconds = 0.0
    for polyline in polylines:
        points = []
        for raw in polyline:
            if len(raw) < 2:
                continue
            x, y = float(raw[0]), float(raw[1])
            if math.isfinite(x) and math.isfinite(y):
                points.append((0x8000 + x * galvos_per_mm, 0x8000 + y * galvos_per_mm))
        for index, (x, y) in enumerate(points):
            command = listMarkTo if (mark and index) else listJumpTo
            seconds += timer.command_seconds(command, x, y)
    return seconds


def estimate_mark_seconds(cfg: MarkConfig, params: JobParams, polylines: list[list[list[float]]]) -> float:
    """Estimated execution time of one mark_polylines pass."""
    galvos_per_mm = 0xFFFF / effective_field_size(cfg)
    return MO_OPEN_SETTLE * 10e-6 + _paths_seconds(polylines, galvos_per_mm, 3000.0, params.mark_speed, True)


def estimate_frame_seconds(cfg: MarkConfig, polylines: list[list[list[float]]]) -> float:
    """Estimated execution time of one frame_polylines run (all its retraces)."""
    galvos_per_mm = 0xFFFF / effective_field_size(cfg)
    points = sum(1 for polyline in polylines for raw in polyline if len(raw) >= 2)
    one_pass = _paths_seconds(polylines, galvos_per_mm, FRAME_LIGHT_SPEED_MM_S, FRAME_LIGHT_SPEED_MM_S, False)
    return frame_once_repeats(polylines, points) * one_pass


def read_board_status() -> dict[str, Any]:
    params = JobParams(
        text="",
        power=1.0,
        frequency_khz=30.0,
        pulse_width_ns=200.0,
        size_mm=10.0,
        mark_speed=100.0,
    )
    cfg = MarkConfig(path=Path("-"), values={"FIELDSIZE": "200"})
    controller = connect_controller(cfg, params)
    try:
        return {
            "version": controller.get_version(),
            "serial": controller.get_serial_number(),
            "list_status": controller.get_list_status(),
        }
    finally:
        controller.shutdown()


def frame_box(cfg: MarkConfig, params: JobParams) -> dict[str, Any]:
    evaluate_emission(
        cfg=cfg,
        power_percent=params.power,
        frequency_khz=params.frequency_khz,
        pulse_width_ns=params.pulse_width_ns,
        intends_emission=False,
        arm=False,
        confirm="",
        operation="frame_only",
        paths_count=1,
    ).raise_if_blocked()
    field_size = effective_field_size(cfg)
    points = box_points(params.size_mm, field_size)
    controller = connect_controller(cfg, params)
    try:
        def job(c: GalvoController) -> bool:
            with c.lighting():
                c.dark(*points[0])
                for point in points[1:]:
                    c.light(*point)
            return True

        _run_job(controller, job)
        return {
            "ok": True,
            "operation": "frame",
            "emission_enabled": False,
            "mock": mock_mode(),
            "points": points,
        }
    finally:
        controller.shutdown()


class FrameLoop:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._controller: GalvoController | None = None
        self._points: list[tuple[int, int]] = []
        self._fault: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._controller is not None

    def start(self, cfg: MarkConfig, params: JobParams, polylines: list[list[list[float]]] | None = None) -> dict[str, Any]:
        evaluate_emission(
            cfg=cfg,
            power_percent=params.power,
            frequency_khz=params.frequency_khz,
            pulse_width_ns=params.pulse_width_ns,
            intends_emission=False,
            arm=False,
            confirm="",
            operation="frame_only",
            paths_count=len(polylines or []) or 1,
        ).raise_if_blocked()
        with self._lock:
            self.stop()
            self._fault = None
            field_size = effective_field_size(cfg)
            job_paths = sanitize_polylines(polylines, field_size) if polylines else [box_points(params.size_mm, field_size)]
            self._points = [pt for path in job_paths for pt in path]
            controller = connect_controller(cfg, params)

            def loop_job(c: GalvoController) -> bool:
                if c._openlaser_stop.is_set():  # type: ignore[attr-defined]
                    # Leave the spooler here; returning True would run
                    # initial_configuration() outside any handler.
                    raise ConnectionAbortedError("stop requested")
                c.lighting_configuration()
                for path in job_paths:
                    c.dark(*path[0])
                    for point in path[1:]:
                        c.light(*point)
                return False

            controller.submit(_guard_job(controller, loop_job, on_fault=lambda reason: self._on_fault(controller, reason)))
            self._controller = controller
            return {
                "ok": True,
                "operation": "frame_loop_start",
                "emission_enabled": False,
                "mock": mock_mode(),
                "points": self._points,
            }

    def _on_fault(self, controller: GalvoController, reason: str) -> None:
        """The board stopped making progress: stop the loop as a user Stop would.

        Runs on the spooler thread, which shutdown() joins, so it hands off.
        """

        def stop_faulted() -> None:
            with self._lock:
                if self._controller is controller:
                    self._controller = None
                    self._points = []
                self._fault = reason
            stop_controller(controller)

        threading.Thread(target=stop_faulted, name="openlaser-frame-fault-stop", daemon=True).start()

    def stop(self) -> dict[str, Any]:
        controller = self._controller
        self._controller = None
        self._points = []
        if controller is not None:
            stop_controller(controller)
        return {
            "ok": True,
            "operation": "frame_loop_stop",
            "emission_enabled": False,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._controller is not None,
                "points": self._points,
                "emission_enabled": False,
                "fault": self._fault,
            }


def mark_box(cfg: MarkConfig, params: JobParams, arm: bool, confirm: str) -> dict[str, Any]:
    safety = evaluate_emission(
        cfg=cfg,
        power_percent=params.power,
        frequency_khz=params.frequency_khz,
        pulse_width_ns=params.pulse_width_ns,
        intends_emission=True,
        arm=arm,
        confirm=confirm,
        operation="vector_engrave",
        paths_count=1,
    )
    safety.raise_if_blocked()

    plan = build_test_box_plan(
        cfg,
        power=params.power,
        frequency_khz=params.frequency_khz,
        pulse_width_ns=params.pulse_width_ns,
        size_mm=params.size_mm,
        arm=True,
    )
    field_size = effective_field_size(cfg)
    points = box_points(params.size_mm, field_size)
    controller = connect_controller(cfg, params)
    try:
        def job(c: GalvoController) -> bool:
            with c.marking():
                c.goto(*points[0])
                for point in points[1:]:
                    c.mark(*point)
                for start_mm, end_mm in text_segments(params.text, params.size_mm):
                    c.goto(*mm_to_galvo(*start_mm, field_size))
                    c.mark(*mm_to_galvo(*end_mm, field_size))
            return True

        _run_job(controller, job)
        return {
            "ok": True,
            "operation": "mark",
            "emission_enabled": True,
            "mock": mock_mode(),
            "plan": plan,
            "points": points,
            "text": params.text,
        }
    finally:
        controller.shutdown()


def frame_polylines(cfg: MarkConfig, params: JobParams, polylines: list[list[list[float]]]) -> dict[str, Any]:
    evaluate_emission(
        cfg=cfg,
        power_percent=params.power,
        frequency_khz=params.frequency_khz,
        pulse_width_ns=params.pulse_width_ns,
        intends_emission=False,
        arm=False,
        confirm="",
        operation="frame_only",
        paths_count=len(polylines or []),
    ).raise_if_blocked()
    field_size = effective_field_size(cfg)
    job_paths = sanitize_polylines(polylines, field_size)
    repeats = frame_once_repeats(polylines, sum(len(path) for path in job_paths))
    controller = connect_controller(cfg, params)
    try:
        def job(c: GalvoController) -> bool:
            with c.lighting():
                for _ in range(repeats):
                    for path in job_paths:
                        c.dark(*path[0])
                        for point in path[1:]:
                            c.light(*point)
            return True

        _run_job(controller, job)
        return {
            "ok": True,
            "operation": "frame_geometry",
            "emission_enabled": False,
            "mock": mock_mode(),
            "repeats": repeats,
            "paths": len(job_paths),
            "points": sum(len(path) for path in job_paths),
        }
    finally:
        controller.shutdown()


def mark_polylines(
    cfg: MarkConfig,
    params: JobParams,
    polylines: list[list[list[float]]],
    arm: bool,
    confirm: str,
    operation: str = "vector_engrave",
) -> dict[str, Any]:
    safety = evaluate_emission(
        cfg=cfg,
        power_percent=params.power,
        frequency_khz=params.frequency_khz,
        pulse_width_ns=params.pulse_width_ns,
        intends_emission=True,
        arm=arm,
        confirm=confirm,
        operation=operation,
        paths_count=len(polylines or []),
    )
    safety.raise_if_blocked()
    field_size = effective_field_size(cfg)
    job_paths = sanitize_polylines(polylines, field_size)
    controller = connect_controller(cfg, params)
    try:
        def job(c: GalvoController) -> bool:
            with c.marking():
                for path in job_paths:
                    c.goto(*path[0])
                    for point in path[1:]:
                        c.mark(*point)
            return True

        _run_job(controller, job)
        return {
            "ok": True,
            "operation": "mark_geometry",
            "emission_enabled": True,
            "mock": mock_mode(),
            "power_percent": params.power,
            "frequency_khz": params.frequency_khz,
            "effective_pulse_width_ns": nearest_pulse_width(params.pulse_width_ns, pulse_width_table(cfg)),
            "paths": len(job_paths),
            "points": sum(len(path) for path in job_paths),
        }
    finally:
        controller.shutdown()
