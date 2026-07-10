"""Machine capabilities and built-in OpenLaser device profiles.

The original application assumed every job targeted a 200 mm JCZ galvo.
OpenLaser keeps that profile, but makes the motion system, tool, work area,
and output dialect explicit so planners do not need machine-name checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


FIBER_GALVO = "fiber_galvo"
CO2_GANTRY = "co2_gantry"
DIODE_GANTRY = "diode_gantry"
PEN_PLOTTER = "pen_plotter"


@dataclass(frozen=True)
class MachineSpec:
    machine_id: str
    name: str
    kind: str
    transport: str
    width_mm: float
    height_mm: float
    origin: str
    tool: str
    output_format: str
    max_speed_mm_s: float
    travel_speed_mm_s: float
    power_scale: int = 1000
    laser_command: str = "M4"
    tool_on_command: str = ""
    tool_off_command: str = "M5"
    supports_air_assist: bool = False
    uses_frequency: bool = False
    uses_pulse_width: bool = False
    direct_control: bool = False
    notes: str = ""

    @property
    def is_laser(self) -> bool:
        return self.tool == "laser"

    @property
    def is_plotter(self) -> bool:
        return self.tool == "pen"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["is_laser"] = self.is_laser
        data["is_plotter"] = self.is_plotter
        return data


BUILTIN_MACHINES: tuple[MachineSpec, ...] = (
    MachineSpec(
        machine_id="jpt-m7-200",
        name="JPT M7 MOPA · 200 mm galvo",
        kind=FIBER_GALVO,
        transport="jcz_lmc_usb",
        width_mm=200.0,
        height_mm=200.0,
        origin="center",
        tool="laser",
        output_format="jcz_lmc",
        max_speed_mm_s=6000.0,
        travel_speed_mm_s=3000.0,
        uses_frequency=True,
        uses_pulse_width=True,
        direct_control=True,
        notes="Existing OpenMopa JCZ/BJJCZ direct-control path.",
    ),
    MachineSpec(
        machine_id="co2-grbl-600x400",
        name="CO₂ cutter · GRBL 600 × 400 mm",
        kind=CO2_GANTRY,
        transport="grbl_serial",
        width_mm=600.0,
        height_mm=400.0,
        origin="bottom_left",
        tool="laser",
        output_format="gcode",
        max_speed_mm_s=500.0,
        travel_speed_mm_s=500.0,
        power_scale=1000,
        laser_command="M3",
        supports_air_assist=True,
        notes="G-code export for GRBL-compatible CO₂ controllers; not Ruida DSP.",
    ),
    MachineSpec(
        machine_id="diode-grbl-400x400",
        name="Diode laser · GRBL 400 × 400 mm",
        kind=DIODE_GANTRY,
        transport="grbl_serial",
        width_mm=400.0,
        height_mm=400.0,
        origin="bottom_left",
        tool="laser",
        output_format="gcode",
        max_speed_mm_s=250.0,
        travel_speed_mm_s=250.0,
        power_scale=1000,
        laser_command="M4",
        supports_air_assist=True,
        notes="Dynamic-power M4 output for GRBL diode controllers.",
    ),
    MachineSpec(
        machine_id="plotter-grbl-a4",
        name="Pen plotter · GRBL A4",
        kind=PEN_PLOTTER,
        transport="grbl_serial",
        width_mm=210.0,
        height_mm=297.0,
        origin="bottom_left",
        tool="pen",
        output_format="gcode",
        max_speed_mm_s=150.0,
        travel_speed_mm_s=150.0,
        power_scale=1000,
        tool_on_command="M3 S1000",
        tool_off_command="M5",
        notes="Uses spindle PWM as pen down/up; commands are isolated in the profile.",
    ),
)

_BY_ID = {machine.machine_id: machine for machine in BUILTIN_MACHINES}
DEFAULT_MACHINE_ID = BUILTIN_MACHINES[0].machine_id


def machine_by_id(machine_id: str | None) -> MachineSpec:
    if not machine_id:
        return _BY_ID[DEFAULT_MACHINE_ID]
    try:
        return _BY_ID[machine_id]
    except KeyError as exc:
        raise ValueError(f"unknown machine profile: {machine_id}") from exc


def machine_catalog() -> list[dict[str, object]]:
    return [machine.to_dict() for machine in BUILTIN_MACHINES]
