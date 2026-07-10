from __future__ import annotations

import argparse
import configparser
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PROFILE = Path(os.environ.get(
    "OPENLASER_PROFILE",
    os.environ.get(
        "OPENMOPA_PROFILE",
        Path(__file__).resolve().parents[1] / "profiles" / "openmopa-machine.ini",
    ),
))
DEFAULT_MARKCFG = DEFAULT_PROFILE

JCZ_VENDOR_ID = 0x9588
JCZ_PRODUCT_ID = 0x9899
JPT_M7_PULSE_WIDTHS_NS = (
    2,
    4,
    6,
    9,
    13,
    20,
    30,
    45,
    55,
    60,
    80,
    100,
    150,
    200,
    250,
    300,
    350,
    400,
    450,
    500,
)


@dataclass(frozen=True)
class MarkConfig:
    path: Path
    values: dict[str, str]

    def get_int(self, key: str, default: int | None = None) -> int | None:
        value = self.values.get(key)
        if value is None:
            return default
        return int(float(value))

    def get_float(self, key: str, default: float | None = None) -> float | None:
        value = self.values.get(key)
        if value is None:
            return default
        return float(value)

    def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        value = self.values.get(key)
        if value is None:
            return default
        return bool(int(float(value)))


def load_markcfg(path: Path) -> MarkConfig:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    text = path.read_text(encoding="latin-1", errors="ignore")
    parser.read_string(text)
    if "LMC_CFG" not in parser:
        raise ValueError(f"{path} does not contain an [LMC_CFG] section")
    return MarkConfig(path=path, values=dict(parser["LMC_CFG"]))


def pulse_width_table(cfg: MarkConfig | None) -> tuple[int, ...]:
    """Pulse widths (ns) this machine accepts.

    Profiles may override the default JPT M7 table with a PULSEWIDTHS key
    ([LMC_CFG], comma-separated integer nanoseconds). Invalid or empty
    values fall back to the M7 table so safety never loosens silently.
    """
    if cfg is None or "PULSEWIDTHS" not in cfg.values:
        return JPT_M7_PULSE_WIDTHS_NS
    try:
        values = tuple(
            sorted({int(float(part.strip())) for part in cfg.values["PULSEWIDTHS"].split(",")})
        )
    except (TypeError, ValueError):
        return JPT_M7_PULSE_WIDTHS_NS
    if not values or any(value <= 0 for value in values):
        return JPT_M7_PULSE_WIDTHS_NS
    return values


def nearest_pulse_width(value: float, table: tuple[int, ...] = JPT_M7_PULSE_WIDTHS_NS) -> int:
    return min(table, key=lambda candidate: abs(candidate - value))


def frequency_to_period(frequency_khz: float, base: float = 20000.0) -> int:
    if frequency_khz <= 0:
        raise ValueError("frequency must be positive")
    return int(round(base / frequency_khz)) & 0xFFFF


def power_to_mark_current(power_percent: float) -> int:
    if not 0 <= power_percent <= 100:
        raise ValueError("power must be between 0 and 100 percent")
    return int(round(power_percent * 0xFFF / 100.0))


def run_command(args: list[str]) -> tuple[int, str]:
    if shutil.which(args[0]) is None:
        return 127, f"{args[0]} not found"
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def pyusb_probe() -> dict[str, Any]:
    if importlib.util.find_spec("usb") is None:
        return {
            "available": False,
            "devices": [],
            "note": "pyusb is not installed for this Python.",
        }
    import usb.core  # type: ignore[import-not-found]

    devices = list(
        usb.core.find(idVendor=JCZ_VENDOR_ID, idProduct=JCZ_PRODUCT_ID, find_all=True)
    )
    return {
        "available": True,
        "devices": [
            {
                "idVendor": f"0x{dev.idVendor:04x}",
                "idProduct": f"0x{dev.idProduct:04x}",
                "bus": getattr(dev, "bus", None),
                "address": getattr(dev, "address", None),
            }
            for dev in devices
        ],
    }


def detect_report() -> dict[str, Any]:
    libusb_path = Path("/opt/homebrew/lib/libusb-1.0.dylib")
    pyusb = pyusb_probe()
    profiler_code, profiler_out = run_command(["system_profiler", "SPUSBDataType"])
    ioreg_code, ioreg_out = run_command(["ioreg", "-p", "IOUSB", "-l", "-w", "0"])

    interesting_terms = ("9588", "9899", "JCZ", "BJJCZ", "LMCV")
    profiler_hits = [
        line.strip()
        for line in profiler_out.splitlines()
        if any(term.lower() in line.lower() for term in interesting_terms)
    ]
    ioreg_hits = [
        line.strip()
        for line in ioreg_out.splitlines()
        if any(term.lower() in line.lower() for term in interesting_terms)
    ]

    connected = bool(pyusb["devices"] or profiler_hits or ioreg_hits)
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "expected_jcz_lmc_usb_id": "0x9588:0x9899",
        "connected": connected,
        "homebrew_libusb_present": libusb_path.exists(),
        "pyusb": pyusb,
        "system_profiler_exit": profiler_code,
        "system_profiler_hits": profiler_hits,
        "ioreg_exit": ioreg_code,
        "ioreg_hits": ioreg_hits,
        "safe_status": "no laser commands sent",
    }


def detect(_: argparse.Namespace) -> int:
    report = detect_report()
    print(json.dumps(report, indent=2))
    if not report["connected"]:
        print(
            "\nNo JCZ/LMC USB device is visible to macOS yet. "
            "Reconnect power/USB or try another cable/adapter, then run detect again."
        )
        return 2
    return 0


def config_summary(cfg: MarkConfig) -> dict[str, Any]:
    keys = (
        "LASERTYPE",
        "FIELDSIZE",
        "MINPWMFREQ",
        "MAXPWMFREQ",
        "m_nIPGSerialNo",
        "m_bEnableIPGSetPulseWidth",
        "m_nEnableFiberLaserStateCheckInMarking",
        "REDLIGHTSPEED",
        "DOORINPORT",
        "STARTMARKPORT",
        "ENABLECORFILE",
        "CORFILE",
    )
    summary = {key: cfg.values.get(key) for key in keys if key in cfg.values}
    summary["path"] = str(cfg.path)
    summary["safe_status"] = "parsed only; machine profile not modified"
    return summary


def show_config(args: argparse.Namespace) -> int:
    cfg = load_markcfg(args.profile)
    summary = config_summary(cfg)
    print(json.dumps(summary, indent=2))
    return 0


def pulse_widths(_: argparse.Namespace) -> int:
    print(json.dumps({"jpt_m7_pulse_widths_ns": JPT_M7_PULSE_WIDTHS_NS}, indent=2))
    return 0


def list_machines(_: argparse.Namespace) -> int:
    from .machines import machine_catalog

    print(json.dumps({"machines": machine_catalog()}, indent=2))
    return 0


def build_test_box_plan(
    cfg: MarkConfig,
    power: float,
    frequency_khz: float,
    pulse_width_ns: float,
    size_mm: float,
    arm: bool = False,
) -> dict[str, Any]:
    min_freq_hz = cfg.get_int("MINPWMFREQ", 1000) or 1000
    max_freq_hz = cfg.get_int("MAXPWMFREQ", 4000000) or 4000000
    requested_freq_hz = frequency_khz * 1000.0
    if requested_freq_hz < min_freq_hz or requested_freq_hz > max_freq_hz:
        raise ValueError(
            f"frequency {requested_freq_hz:.0f} Hz outside machine profile bounds "
            f"{min_freq_hz}..{max_freq_hz} Hz"
        )
    if power < 0.0 or power > 100.0:
        raise ValueError("test power must be between 0% and 100%.")
    table = pulse_width_table(cfg)
    snapped_pw = nearest_pulse_width(pulse_width_ns, table)
    commands = [
        {
            "name": "listMarkCurrent",
            "opcode": "0x8012",
            "value": power_to_mark_current(power),
            "source": f"{power:.3g}% power",
        },
        {
            "name": "listQSwitchPeriod",
            "opcode": "0x801b",
            "value": frequency_to_period(frequency_khz),
            "source": f"{frequency_khz:.3g} kHz",
        },
        {
            "name": "listFiberYLPMPulseWidth",
            "opcode": "0x8026",
            "value": snapped_pw,
            "source": f"{pulse_width_ns:.3g} ns requested",
        },
        {
            "name": "geometry",
            "shape": "box",
            "size_mm": size_mm,
            "origin": "center",
        },
    ]
    return {
        "mode": "armed" if arm else "dry-run",
        "emission_enabled": False,
        "reason": "hardware emission is intentionally not implemented in this first prototype",
        "profile": str(cfg.path),
        "field_size_mm": cfg.get_float("FIELDSIZE"),
        "power_percent": power,
        "frequency_khz": frequency_khz,
        "requested_pulse_width_ns": pulse_width_ns,
        "effective_pulse_width_ns": snapped_pw,
        "pulse_width_was_snapped": snapped_pw != pulse_width_ns,
        "commands": commands,
    }


def plan_test_box(args: argparse.Namespace) -> int:
    cfg = load_markcfg(args.profile)
    plan = build_test_box_plan(
        cfg,
        power=args.power,
        frequency_khz=args.frequency_khz,
        pulse_width_ns=args.pulse_width_ns,
        size_mm=args.size_mm,
        arm=args.arm,
    )
    print(json.dumps(plan, indent=2))
    if args.arm:
        print("\n--arm was accepted for planning only. No USB commands were sent.")
    return 0


def serve_ui_command(args: argparse.Namespace) -> int:
    from .ui import serve

    serve(host=args.host, port=args.port, markcfg=args.profile)
    return 0


def inspect_profile(args: argparse.Namespace) -> int:
    from .profile import inspect_path

    print(json.dumps(inspect_path(args.profile), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openlaser",
        description="OpenLaser: safety-first jobs for galvo, gantry laser, and plotter machines.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("detect", help="Check local USB/runtime readiness without laser commands.")
    p.set_defaults(func=detect)

    p = sub.add_parser("show-config", help="Parse the machine profile and print mapped fields.")
    p.add_argument("--profile", "--markcfg", dest="profile", metavar="PROFILE", type=Path, default=DEFAULT_PROFILE)
    p.set_defaults(func=show_config)

    p = sub.add_parser("pulse-widths", help="Print the guarded JPT M7 pulse-width table.")
    p.set_defaults(func=pulse_widths)

    p = sub.add_parser("machines", help="List built-in machine profiles and capabilities.")
    p.set_defaults(func=list_machines)

    p = sub.add_parser("plan-test-box", help="Plan a test box without emission.")
    p.add_argument("--profile", "--markcfg", dest="profile", metavar="PROFILE", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--power", type=float, default=100.0, help="Power percent; valid range is 0..100.")
    p.add_argument("--frequency-khz", type=float, default=30.0)
    p.add_argument("--pulse-width-ns", type=float, default=200.0)
    p.add_argument("--size-mm", type=float, default=10.0)
    p.add_argument("--arm", action="store_true", help="Accepted for planning; still no emission.")
    p.set_defaults(func=plan_test_box)

    p = sub.add_parser("ui", help="Open the local dry-run web UI.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--profile", "--markcfg", dest="profile", metavar="PROFILE", type=Path, default=DEFAULT_PROFILE)
    p.set_defaults(func=serve_ui_command)

    p = sub.add_parser("inspect-profile", help="Report which machine profile fields are applied/unused.")
    p.add_argument("--profile", "--markcfg", dest="profile", metavar="PROFILE", type=Path, default=DEFAULT_PROFILE)
    p.set_defaults(func=inspect_profile)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
