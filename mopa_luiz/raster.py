"""Raster engraving: fill closed regions with hatch lines.

This is the same hatch/fill approach common in laser/CAD software:

  1. Identify closed polylines (regions). Open paths are skipped.
  2. Rotate every region so the hatch direction becomes horizontal.
  3. Sweep horizontal scan lines across the rotated bounding box.
     Each scan line intersects the region polylines at 0, 2, 4, ...
     points (assuming a well-formed even-odd fill rule). Sort the
     intersections by x and emit hatch segments between pairs:
     0..1 is "inside", 1..2 is "in a hole", 2..3 is "inside again", ...
     Even-odd handles letter holes naturally (the inside of an "O"
     and the triangular hole of an "A" both fall on the wrong side
     of the count and are skipped).
  4. Rotate the resulting hatch segments back to world coordinates.
  5. For multi-pass output (cross-hatch), repeat with `angle_step_deg`
     added per pass — angle 0 then 90 gives a checkerboard fill.
  6. Boustrophedon order: alternate the direction of every other scan
     line so the galvo doesn't need to fly all the way back to the
     start of each row.

The output is a flat list of 2-point polylines that the existing
`mark_polylines` can mark in order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

Point = tuple[float, float]
Polyline = list[Point]


@dataclass
class RasterResult:
    segments: list[Polyline]
    closed_input_count: int
    skipped_open_count: int
    passes: int
    pitch_mm: float
    angles_deg: list[float]

    def to_dict(self) -> dict[str, object]:
        return {
            "segments": [[list(p) for p in seg] for seg in self.segments],
            "segment_count": len(self.segments),
            "closed_input_count": self.closed_input_count,
            "skipped_open_count": self.skipped_open_count,
            "passes": self.passes,
            "pitch_mm": self.pitch_mm,
            "angles_deg": list(self.angles_deg),
        }


def is_closed(polyline: Sequence[Point], tol: float = 1e-3) -> bool:
    if len(polyline) < 3:
        return False
    ax, ay = polyline[0]
    bx, by = polyline[-1]
    return math.hypot(ax - bx, ay - by) <= tol


def hatch_polylines(
    polylines: Sequence[Sequence[Point]],
    angle_deg: float = 0.0,
    pitch_mm: float = 0.1,
    passes: int = 1,
    angle_step_deg: float = 90.0,
    closure_tol_mm: float = 0.05,
) -> RasterResult:
    """Generate hatch fill segments for the closed regions in `polylines`."""
    if pitch_mm <= 0:
        raise ValueError("raster pitch must be > 0")
    if passes < 1:
        return RasterResult(segments=[], closed_input_count=0, skipped_open_count=0,
                            passes=0, pitch_mm=pitch_mm, angles_deg=[])

    closed: list[Polyline] = []
    skipped = 0
    for poly in polylines:
        pts = [(float(x), float(y)) for x, y in poly]
        if is_closed(pts, tol=closure_tol_mm):
            closed.append(pts)
        else:
            skipped += 1

    if not closed:
        return RasterResult(segments=[], closed_input_count=0,
                            skipped_open_count=skipped, passes=passes,
                            pitch_mm=pitch_mm, angles_deg=[])

    angles = [angle_deg + i * angle_step_deg for i in range(passes)]
    all_segments: list[Polyline] = []
    for angle in angles:
        all_segments.extend(_hatch_one_pass(closed, angle, pitch_mm))

    return RasterResult(
        segments=all_segments,
        closed_input_count=len(closed),
        skipped_open_count=skipped,
        passes=passes,
        pitch_mm=pitch_mm,
        angles_deg=angles,
    )


def _hatch_one_pass(closed_polylines: list[Polyline], angle_deg: float, pitch: float) -> list[Polyline]:
    rotate_in = math.radians(-angle_deg)
    cin, sin_in = math.cos(rotate_in), math.sin(rotate_in)
    rotate_out = math.radians(angle_deg)
    cout, sout = math.cos(rotate_out), math.sin(rotate_out)

    rotated: list[Polyline] = [
        [(x * cin - y * sin_in, x * sin_in + y * cin) for x, y in poly]
        for poly in closed_polylines
    ]

    min_y = min(p[1] for poly in rotated for p in poly)
    max_y = max(p[1] for poly in rotated for p in poly)

    # Offset by a small fraction of pitch so the scan never exactly hits a
    # vertex — that would produce odd numbers of intersections and ragged fills.
    offset = pitch * 0.0173
    y_start = math.floor(min_y / pitch) * pitch + offset

    segments: list[Polyline] = []
    flip = False
    y = y_start
    while y < max_y + pitch:
        xs: list[float] = []
        for poly in rotated:
            for i in range(len(poly) - 1):
                ax, ay = poly[i]
                bx, by = poly[i + 1]
                if ay == by:
                    continue
                lo, hi = (ay, by) if ay < by else (by, ay)
                # Half-open interval keeps shared vertices from being counted twice.
                if y < lo or y >= hi:
                    continue
                t = (y - ay) / (by - ay)
                xs.append(ax + t * (bx - ax))
        xs.sort()
        pairs = []
        for i in range(0, len(xs) - 1, 2):
            x0, x1 = xs[i], xs[i + 1]
            if x1 - x0 > 1e-6:
                pairs.append((x0, x1))
        if flip:
            pairs.reverse()
        for x0, x1 in pairs:
            if flip:
                x0, x1 = x1, x0
            p0 = (x0 * cout - y * sout, x0 * sout + y * cout)
            p1 = (x1 * cout - y * sout, x1 * sout + y * cout)
            segments.append([p0, p1])
        flip = not flip
        y += pitch

    return segments
