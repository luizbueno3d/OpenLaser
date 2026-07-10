"""Single source of truth for laser-emission safety policy.

All paths that can cause laser emission MUST go through `evaluate_emission`
or one of its narrower wrappers before contacting the controller.

What this module enforces today:
  - Power must lie within the laser's electrical 0..100% range.
  - Frequency must lie within the machine profile's MINPWMFREQ / MAXPWMFREQ bounds.
  - Pulse width must land on the machine profile's PULSEWIDTHS table,
    defaulting to the JPT M7 table (within rounding).
  - Emission paths must carry an explicit `arm=True` and the literal
    confirmation token "ARM".
  - The job must have at least one markable path.

There is no artificial cap on commanded power; the operator selects the
working power via per-layer settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cli import MarkConfig, nearest_pulse_width, pulse_width_table


ARM_TOKEN = "ARM"


@dataclass
class SafetyResult:
    ok: bool
    emission_will_occur: bool
    arm_ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def raise_if_blocked(self) -> None:
        if not self.ok:
            raise PermissionError("; ".join(self.errors) or "blocked by safety policy")

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "emission_will_occur": self.emission_will_occur,
            "arm_ok": self.arm_ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def evaluate_emission(
    *,
    cfg: MarkConfig | None,
    power_percent: float,
    frequency_khz: float,
    pulse_width_ns: float,
    intends_emission: bool,
    arm: bool,
    confirm: str,
    operation: str = "vector",
    paths_count: int = 0,
) -> SafetyResult:
    """Decide whether a frame/mark request is allowed."""
    arm_ok = bool(arm) and confirm.strip().upper() == ARM_TOKEN

    errors: list[str] = []
    warnings: list[str] = []

    if power_percent < 0 or power_percent > 100:
        errors.append("power must be between 0 and 100 percent")

    if cfg is not None:
        min_freq_hz = cfg.get_int("MINPWMFREQ", 1000) or 1000
        max_freq_hz = cfg.get_int("MAXPWMFREQ", 4000000) or 4000000
        requested_freq_hz = frequency_khz * 1000.0
        if requested_freq_hz < min_freq_hz or requested_freq_hz > max_freq_hz:
            errors.append(
                f"frequency {requested_freq_hz:.0f} Hz outside machine profile bounds "
                f"{min_freq_hz}..{max_freq_hz} Hz"
            )

    table = pulse_width_table(cfg)
    if pulse_width_ns not in table:
        snapped = nearest_pulse_width(pulse_width_ns, table)
        if abs(snapped - pulse_width_ns) > 0.5:
            errors.append(
                f"pulse width {pulse_width_ns} ns not in machine pulse-width table "
                f"(nearest {snapped} ns)"
            )
        else:
            warnings.append(f"pulse width snapped to {snapped} ns")

    if intends_emission and not arm_ok:
        errors.append('emission requires arm=true and confirmation text "ARM"')

    if intends_emission and paths_count <= 0:
        errors.append("no markable geometry in job")

    return SafetyResult(
        ok=not errors,
        emission_will_occur=intends_emission and not errors,
        arm_ok=arm_ok,
        errors=errors,
        warnings=warnings,
    )


def validate_layer_settings(
    *,
    operation: str,
    power_percent: float,
    frequency_khz: float,
    pulse_width_ns: float,
    speed_mm_s: float,
    cfg: MarkConfig | None,
) -> list[str]:
    """Check a layer's parameters for sanity.

    Returns a list of error strings; empty list means OK. `disabled` and
    `frame_only` layers skip validation since they cannot emit.
    """
    errors: list[str] = []
    if operation in {"disabled", "frame_only"}:
        return errors
    if power_percent < 0 or power_percent > 100:
        errors.append(f"layer power {power_percent} out of 0..100")
    if speed_mm_s <= 0:
        errors.append("layer speed must be > 0 mm/s")
    if cfg is not None:
        min_freq_hz = cfg.get_int("MINPWMFREQ", 1000) or 1000
        max_freq_hz = cfg.get_int("MAXPWMFREQ", 4000000) or 4000000
        requested_freq_hz = frequency_khz * 1000.0
        if requested_freq_hz < min_freq_hz or requested_freq_hz > max_freq_hz:
            errors.append(
                f"layer frequency {requested_freq_hz:.0f} Hz outside machine profile "
                f"{min_freq_hz}..{max_freq_hz} Hz"
            )
    table = pulse_width_table(cfg)
    if pulse_width_ns not in table:
        snapped = nearest_pulse_width(pulse_width_ns, table)
        if abs(snapped - pulse_width_ns) > 0.5:
            errors.append(
                f"layer pulse width {pulse_width_ns} ns not in machine pulse-width table"
            )
    return errors
