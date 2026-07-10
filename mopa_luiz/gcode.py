"""Compile OpenLaser layer jobs to conservative, controller-neutral G-code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .layers import DISABLED, FRAME_ONLY, RASTER_ENGRAVE, LayerJob
from .machines import MachineSpec
from .raster import hatch_polylines


@dataclass(frozen=True)
class GCodeProgram:
    machine_id: str
    content: str
    path_count: int
    point_count: int
    estimated_distance_mm: float

    def to_dict(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "content": self.content,
            "path_count": self.path_count,
            "point_count": self.point_count,
            "estimated_distance_mm": round(self.estimated_distance_mm, 3),
        }


def _number(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _xy(machine: MachineSpec, point: tuple[float, float]) -> tuple[float, float]:
    """Convert center-origin design millimetres into machine coordinates."""
    x, y = point
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("job contains a non-finite coordinate")
    if machine.origin == "center":
        machine_x, machine_y = x, y
        min_x, max_x = -machine.width_mm / 2.0, machine.width_mm / 2.0
        min_y, max_y = -machine.height_mm / 2.0, machine.height_mm / 2.0
    elif machine.origin == "bottom_left":
        machine_x = x + machine.width_mm / 2.0
        machine_y = y + machine.height_mm / 2.0
        min_x, max_x = 0.0, machine.width_mm
        min_y, max_y = 0.0, machine.height_mm
    elif machine.origin == "top_left":
        machine_x = x + machine.width_mm / 2.0
        machine_y = machine.height_mm / 2.0 - y
        min_x, max_x = 0.0, machine.width_mm
        min_y, max_y = 0.0, machine.height_mm
    else:
        raise ValueError(f"unsupported machine origin: {machine.origin}")
    tolerance = 1e-6
    if not (
        min_x - tolerance <= machine_x <= max_x + tolerance
        and min_y - tolerance <= machine_y <= max_y + tolerance
    ):
        raise ValueError(
            f"geometry point ({x:.3f}, {y:.3f}) mm is outside the "
            f"{machine.width_mm:g} × {machine.height_mm:g} mm work area"
        )
    return machine_x, machine_y


def _distance(points: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _paths_for_job(job: LayerJob) -> list[list[tuple[float, float]]]:
    if job.layer.operation != RASTER_ENGRAVE:
        return job.polylines
    result = hatch_polylines(
        [[list(point) for point in polyline] for polyline in job.polylines],
        angle_deg=job.layer.raster_angle_deg,
        pitch_mm=job.layer.raster_pitch_mm,
        passes=max(1, job.layer.passes),
        angle_step_deg=job.layer.raster_angle_step_deg,
    )
    return [[(float(x), float(y)) for x, y in segment] for segment in result.segments]


def compile_gcode(machine: MachineSpec, jobs: list[LayerJob]) -> GCodeProgram:
    if machine.output_format != "gcode":
        raise ValueError(f"{machine.name} does not use G-code output")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "; OpenLaser G-code",
        f"; Machine: {machine.name}",
        f"; Generated: {now}",
        "; Design origin: center; units: millimetres",
        "G21",
        "G90",
        machine.tool_off_command,
    ]
    path_count = 0
    point_count = 0
    distance_mm = 0.0

    for job in jobs:
        layer = job.layer
        if not layer.output or layer.operation in {DISABLED, FRAME_ONLY}:
            continue
        raw_paths = _paths_for_job(job)
        if not raw_paths:
            continue
        passes = 1 if layer.operation == RASTER_ENGRAVE else max(1, layer.passes)
        speed = min(float(layer.speed_mm_s), machine.max_speed_mm_s)
        if speed <= 0:
            raise ValueError(f"layer {layer.name!r} speed must be greater than zero")
        feed = speed * 60.0
        power = float(layer.power_percent)
        if not 0.0 <= power <= 100.0:
            raise ValueError(f"layer {layer.name!r} power must be between 0 and 100 percent")
        scaled_power = int(round(machine.power_scale * power / 100.0))
        lines.append(f"; Layer: {layer.name} · {layer.operation}")
        if machine.is_laser and layer.air_assist and machine.supports_air_assist:
            lines.append("M8")

        for pass_index in range(passes):
            if passes > 1:
                lines.append(f"; Pass {pass_index + 1}/{passes}")
            for raw_path in raw_paths:
                if len(raw_path) < 2:
                    continue
                path = [_xy(machine, (float(x), float(y))) for x, y in raw_path]
                start_x, start_y = path[0]
                lines.append(machine.tool_off_command)
                lines.append(f"G0 X{_number(start_x)} Y{_number(start_y)}")
                if machine.is_plotter:
                    lines.append(machine.tool_on_command)
                else:
                    lines.append(f"{machine.laser_command} S{scaled_power}")
                for x, y in path[1:]:
                    lines.append(f"G1 X{_number(x)} Y{_number(y)} F{_number(feed)}")
                lines.append(machine.tool_off_command)
                path_count += 1
                point_count += len(path)
                distance_mm += _distance(path)
        if machine.is_laser and layer.air_assist and machine.supports_air_assist:
            lines.append("M9")

    if path_count == 0:
        raise ValueError("no output-enabled geometry for G-code export")
    lines.extend([machine.tool_off_command, "M9", "G0 X0 Y0", "; End OpenLaser job", ""])
    return GCodeProgram(
        machine_id=machine.machine_id,
        content="\n".join(lines),
        path_count=path_count,
        point_count=point_count,
        estimated_distance_mm=distance_mm,
    )
