"""Inspect which markcfg parameters are parsed, applied, parsed-but-unused, or missing.

Tracks the keys we know about and tags each one with how the project uses it.
This is intentionally explicit — every "applied" tag is something a real
code path reads today, not aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli import MarkConfig, load_markcfg


APPLIED = "applied"
PARSED_UNUSED = "parsed but not applied"
NOT_FOUND = "not found"


@dataclass
class ProfileField:
    key: str
    label: str
    expected: bool
    used_by: tuple[str, ...]
    notes: str = ""


# `used_by` is the truthful list of code paths that actually read this key.
KNOWN_FIELDS: tuple[ProfileField, ...] = (
    ProfileField("FIELDSIZE", "Galvo field size (mm)", True,
                 ("live.connect_controller", "live.sanitize_polylines",
                  "ui.config_summary"),
                 notes="Drives mm<->galvo unit conversion."),
    ProfileField("MINPWMFREQ", "Min PWM/Q-switch frequency (Hz)", True,
                 ("safety.evaluate_emission", "cli.build_test_box_plan")),
    ProfileField("MAXPWMFREQ", "Max PWM/Q-switch frequency (Hz)", True,
                 ("safety.evaluate_emission", "cli.build_test_box_plan")),
    ProfileField("m_bEnableIPGSetPulseWidth", "Enable IPG/JPT pulse-width set", True,
                 (),  # parsed/displayed only — pulse-width is sent via galvoplotter
                 notes="Surfaced in UI; pulse-width emission is unconditional via galvoplotter."),
    ProfileField("m_nIPGSerialNo", "IPG/JPT serial preset", True, (),
                 notes="Display only; informs that this is a JPT M7 profile."),
    ProfileField("m_nEnableFiberLaserStateCheckInMarking", "Fiber laser state-check in marking", True, (),
                 notes="Parsed from the machine profile; not enforced in this prototype."),
    ProfileField("LASERTYPE", "Laser type code", False, (),
                 notes="Reported in show-config when present."),
    ProfileField("REDLIGHTSPEED", "Red-light framing speed", False, (),
                 notes="Display only; framing speed is set via JobParams.mark_speed."),
    ProfileField("DOORINPORT", "Safety door input port", False, (),
                 notes="Not consulted in this prototype; macOS host has no IO board access."),
    ProfileField("STARTMARKPORT", "External start-mark input", False, (),
                 notes="Not wired up."),
    ProfileField("ENABLECORFILE", "Use galvo correction file", False, (),
                 notes="Correction file is not loaded by this prototype."),
    ProfileField("CORFILE", "Correction file path", False, (),
                 notes="Display only."),
    ProfileField("MARKDELAY", "Mark delay (us)", False, (),
                 notes="Galvoplotter applies its own delay defaults."),
    ProfileField("JUMPDELAY", "Jump delay (us)", False, ()),
    ProfileField("LASERONDELAY", "Laser on delay (us)", False, ()),
    ProfileField("LASEROFFDELAY", "Laser off delay (us)", False, ()),
    ProfileField("POLYGONDELAY", "Polygon delay (us)", False, ()),
    ProfileField("INVERTX", "Invert X axis", False, ()),
    ProfileField("INVERTY", "Invert Y axis", False, ()),
    ProfileField("SWAPXY", "Swap XY axis", False, ()),
    ProfileField("GALVOASPECT", "Galvo aspect/distortion", False, ()),
)


def inspect(cfg: MarkConfig) -> dict[str, Any]:
    rows = []
    for field in KNOWN_FIELDS:
        raw = cfg.values.get(field.key)
        if raw is None:
            status = NOT_FOUND
        elif field.used_by:
            status = APPLIED
        else:
            status = PARSED_UNUSED
        rows.append({
            "key": field.key,
            "label": field.label,
            "value": raw,
            "status": status,
            "used_by": list(field.used_by),
            "notes": field.notes,
        })
    summary = {APPLIED: 0, PARSED_UNUSED: 0, NOT_FOUND: 0}
    for row in rows:
        summary[row["status"]] += 1
    return {
        "path": str(cfg.path),
        "summary": summary,
        "fields": rows,
    }


def inspect_path(path: Path) -> dict[str, Any]:
    return inspect(load_markcfg(path))
