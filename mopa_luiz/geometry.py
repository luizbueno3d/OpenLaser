"""Geometry helpers: bounding boxes and join-connected-lines.

Polylines are `list[list[(x_mm, y_mm)]]`. The join algorithm builds an
endpoint graph at the supplied tolerance, walks each chain through nodes of
degree <= 2, and stops at branching nodes (degree > 2) so a T-junction does
not collapse into one nonsense path. Closed loops are detected when the
chain returns to its starting node.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

Point = tuple[float, float]
Polyline = list[Point]


@dataclass
class JoinResult:
    polylines: list[Polyline]
    input_segments: int
    output_paths: int
    joins_performed: int
    closed_loops: int
    tolerance_mm: float

    def to_dict(self) -> dict[str, object]:
        return {
            "input_segments": self.input_segments,
            "output_paths": self.output_paths,
            "joins_performed": self.joins_performed,
            "closed_loops": self.closed_loops,
            "tolerance_mm": self.tolerance_mm,
        }


def bounding_box(polylines: Sequence[Sequence[Point]]) -> dict[str, float]:
    xs: list[float] = []
    ys: list[float] = []
    for poly in polylines:
        for x, y in poly:
            xs.append(float(x))
            ys.append(float(y))
    if not xs:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0,
                "width": 0.0, "height": 0.0}
    return {
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


class _NodeMap:
    """Cluster endpoints into integer node ids within `tolerance`."""

    def __init__(self, tolerance: float) -> None:
        self.tolerance = max(tolerance, 1e-9)
        self.buckets: dict[tuple[int, int], list[int]] = {}
        self.centroids: list[Point] = []

    def _bucket(self, point: Point) -> tuple[int, int]:
        return (round(point[0] / self.tolerance), round(point[1] / self.tolerance))

    def node_id(self, point: Point) -> int:
        bx, by = self._bucket(point)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for nid in self.buckets.get((bx + dx, by + dy), ()):
                    cx, cy = self.centroids[nid]
                    if math.hypot(cx - point[0], cy - point[1]) <= self.tolerance:
                        return nid
        nid = len(self.centroids)
        self.centroids.append((float(point[0]), float(point[1])))
        self.buckets.setdefault((bx, by), []).append(nid)
        return nid


def join_lines(
    polylines: Sequence[Sequence[Point]],
    tolerance_mm: float = 0.05,
) -> JoinResult:
    """Merge polylines whose endpoints touch within `tolerance_mm`.

    Internal vertices of input polylines are preserved; only endpoints
    participate in join decisions.
    """
    if tolerance_mm < 0:
        raise ValueError("tolerance must be >= 0")

    segments: list[Polyline] = [
        [(float(x), float(y)) for x, y in poly]
        for poly in polylines
        if len(poly) >= 2
    ]
    if not segments:
        return JoinResult(polylines=[], input_segments=0, output_paths=0,
                          joins_performed=0, closed_loops=0,
                          tolerance_mm=tolerance_mm)

    nodes = _NodeMap(tolerance_mm)
    seg_endpoints: list[tuple[int, int]] = []
    incidence: dict[int, list[int]] = {}

    for index, seg in enumerate(segments):
        a = nodes.node_id(seg[0])
        b = nodes.node_id(seg[-1])
        seg_endpoints.append((a, b))
        incidence.setdefault(a, []).append(index)
        if a != b:
            incidence.setdefault(b, []).append(index)

    consumed = [False] * len(segments)
    output: list[Polyline] = []
    joins = 0
    closed = 0

    def pick_neighbor(node: int) -> int | None:
        if len(incidence.get(node, [])) > 2:
            return None
        for seg_idx in incidence.get(node, []):
            if not consumed[seg_idx]:
                return seg_idx
        return None

    for seed in range(len(segments)):
        if consumed[seed]:
            continue
        consumed[seed] = True
        chain = list(segments[seed])
        head_node, tail_node = seg_endpoints[seed]

        # Extend forward off `tail_node`.
        while head_node != tail_node:
            nxt = pick_neighbor(tail_node)
            if nxt is None:
                break
            consumed[nxt] = True
            seg = list(segments[nxt])
            na, nb = seg_endpoints[nxt]
            if na == tail_node:
                new_tail = nb
            else:
                seg = list(reversed(seg))
                new_tail = na
            chain.extend(seg[1:])
            tail_node = new_tail
            joins += 1

        # Extend backward off `head_node`.
        while head_node != tail_node:
            prev = pick_neighbor(head_node)
            if prev is None:
                break
            consumed[prev] = True
            seg = list(segments[prev])
            na, nb = seg_endpoints[prev]
            if nb == head_node:
                new_head = na
            else:
                seg = list(reversed(seg))
                new_head = nb
            chain[:0] = seg[:-1]
            head_node = new_head
            joins += 1

        if head_node == tail_node and len(chain) >= 3:
            if chain[0] != chain[-1]:
                chain.append(chain[0])
            closed += 1

        output.append(chain)

    return JoinResult(
        polylines=output,
        input_segments=len(segments),
        output_paths=len(output),
        joins_performed=joins,
        closed_loops=closed,
        tolerance_mm=tolerance_mm,
    )
