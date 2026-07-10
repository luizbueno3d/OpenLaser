from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .cli import build_test_box_plan, config_summary, detect_report, load_markcfg, pyusb_probe
from .geometry import bounding_box, join_lines
from .gcode import compile_gcode
from .importers import import_geometry
from .raster import hatch_polylines
from .layers import (
    LayerSpec,
    default_layers,
    emitting_jobs,
    framing_jobs,
    group_by_layer,
    layer_from_dict,
    validate_layers,
)
from .live import (
    FrameLoop,
    JobParams,
    frame_box,
    frame_polylines,
    mark_box,
    mark_polylines,
    read_board_status,
    run_hardware_job,
    clear_stop_request,
    stop_requested,
    stop_active_hardware_jobs,
    load_scale_correction,
    save_scale_correction,
    mock_mode,
)
from .machines import machine_by_id, machine_catalog
from .profile import inspect as inspect_profile_data
from .safety import evaluate_emission


FRAME_LOOP = FrameLoop()
LAYER_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "layer_settings.json"
WORKSPACE_PATH = Path(__file__).resolve().parent.parent / "workspace.json"
WORKSPACE_MAX_OBJECTS = 3000
WORKSPACE_MAX_TOTAL_POINTS = 500_000


def load_saved_layers() -> list[dict[str, object]]:
    if not LAYER_SETTINGS_PATH.exists():
        return [layer.to_dict() for layer in default_layers()]
    with LAYER_SETTINGS_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_layers = payload.get("layers") if isinstance(payload, dict) else payload
    if not isinstance(raw_layers, list):
        return [layer.to_dict() for layer in default_layers()]
    return [layer_from_dict(item).to_dict() for item in raw_layers if isinstance(item, dict)]


def save_layers(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        raise ValueError("layers must be a list")
    layers = [layer_from_dict(item).to_dict() for item in payload if isinstance(item, dict)]
    if not layers:
        raise ValueError("at least one layer is required")
    LAYER_SETTINGS_PATH.write_text(json.dumps({"layers": layers}, indent=2) + "\n", encoding="utf-8")
    return layers


def _finite_float(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def sanitize_workspace_objects(payload: object) -> list[dict[str, object]]:
    """Validate client objects down to the persisted shape. Raises ValueError."""
    if not isinstance(payload, list):
        raise ValueError("objects must be a list")
    if len(payload) > WORKSPACE_MAX_OBJECTS:
        raise ValueError("workspace too large")

    objects: list[dict[str, object]] = []
    total_points = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        polylines: list[list[list[float]]] = []
        for polyline in item.get("polylines") if isinstance(item.get("polylines"), list) else []:
            if not isinstance(polyline, list):
                continue
            clean_polyline: list[list[float]] = []
            for point in polyline:
                if not isinstance(point, list) or len(point) < 2:
                    continue
                x = _finite_float(point[0])
                y = _finite_float(point[1])
                if x is None or y is None:
                    continue
                clean_polyline.append([x, y])
            if len(clean_polyline) >= 2:
                polylines.append(clean_polyline)
                total_points += len(clean_polyline)
                if total_points > WORKSPACE_MAX_TOTAL_POINTS:
                    raise ValueError("workspace too large")
        if not polylines:
            continue
        objects.append({
            "name": item["name"][:200] if isinstance(item.get("name"), str) else "object",
            "layer_id": item.get("layer_id") if isinstance(item.get("layer_id"), str) else "vector-engrave",
            "polylines": polylines,
            "pointCount": sum(len(polyline) for polyline in polylines),
            "x": _finite_float(item.get("x"), 0.0),
            "y": _finite_float(item.get("y"), 0.0),
            "scale": _finite_float(item.get("scale"), 1.0),
            "rotation": _finite_float(item.get("rotation"), 0.0),
        })
    return objects


def load_workspace() -> dict[str, object]:
    try:
        with WORKSPACE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {"objects": sanitize_workspace_objects(data.get("objects")), "saved_at": data.get("saved_at")}
    except Exception:
        return {"objects": [], "saved_at": None}


def save_workspace(payload: object) -> dict[str, object]:
    objects = sanitize_workspace_objects(payload)
    data = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "objects": objects,
    }
    tmp = WORKSPACE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, WORKSPACE_PATH)
    return {"ok": True, "objects": len(objects), "path": str(WORKSPACE_PATH)}


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenLaser</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101314;
      --panel: #191e20;
      --panel2: #111617;
      --line: #30383a;
      --line2: #435052;
      --ink: #eef2ea;
      --muted: #93a09c;
      --accent: #26d0a8;
      --accent2: #e2b84b;
      --danger: #e04848;
      --good: #4ade80;
      --bad: #ef4444;
      --field: #d8d0be;
      --fieldLine: rgba(31, 42, 43, .16);
      --mark: #0c2625;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(rgba(255,255,255,.02) 1px, transparent 1px),
        var(--bg);
      background-size: 22px 22px;
      color: var(--ink);
      font-family: Avenir Next, Helvetica Neue, Helvetica, sans-serif;
      letter-spacing: 0;
      overflow: hidden;
    }
    header {
      height: 52px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: #0d1011;
      gap: 12px;
    }
    h1 {
      margin: 0;
      font-size: 16px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    header .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 5px 9px;
      border: 1px solid var(--line2);
      border-radius: 999px;
      color: var(--muted);
      font-size: 12px;
    }
    header .pill.warning { color: var(--accent2); border-color: var(--accent2); }
    header .pill.bad { color: var(--bad); border-color: var(--bad); }
    .brandLockup { display: flex; align-items: baseline; gap: 9px; white-space: nowrap; }
    .brandLockup small {
      color: var(--muted); font: 600 10px SFMono-Regular, Consolas, monospace;
      letter-spacing: .08em; text-transform: uppercase;
    }
    .machineRail {
      display: flex; align-items: center; gap: 8px; padding: 4px 6px 4px 10px;
      border-left: 3px solid var(--accent2); background: #151a1b;
    }
    .machineRail label { display: block; margin: 0; color: var(--accent2); }
    .machineRail select { min-height: 30px; min-width: 245px; border-radius: 3px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); }
    .ok .dot { background: var(--good); }
    main {
      height: calc(100vh - 52px);
      display: grid;
      grid-template-columns: 288px minmax(480px, 1fr) 342px;
    }
    main.leftCollapsed { grid-template-columns: minmax(0, 1fr) 342px; }
    main.rightCollapsed { grid-template-columns: 288px minmax(0, 1fr); }
    main.leftCollapsed.rightCollapsed { grid-template-columns: minmax(0, 1fr); }
    main.leftCollapsed .leftPanel,
    main.rightCollapsed .rightPanel { display: none; }
    aside, .right {
      overflow: auto;
      border-right: 1px solid var(--line);
      background: rgba(17, 22, 23, .94);
      padding: 13px;
    }
    .right { border-right: 0; border-left: 1px solid var(--line); }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      margin-bottom: 11px;
    }
    h2 {
      margin: 0 0 9px;
      font-size: 11px;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: .12em;
    }
    details summary {
      cursor: pointer;
      list-style: none;
    }
    details summary::-webkit-details-marker { display: none; }
    details summary h2::after {
      content: '  ▸';
      color: var(--muted);
    }
    details[open] summary h2::after { content: '  ▾'; }
    .info {
      margin: 0 0 10px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #0d1112;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .info strong {
      display: block;
      color: var(--ink);
      margin-bottom: 4px;
    }
    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 9px;
    }
    input, select {
      width: 100%;
      min-height: 31px;
      border: 1px solid var(--line2);
      border-radius: 6px;
      background: #0e1213;
      color: var(--ink);
      padding: 6px 8px;
      font: inherit;
      font-size: 12px;
    }
    input[type=file] { padding: 6px; }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 7px; }
    button {
      min-height: 32px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #061110;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }
    button.secondary {
      background: transparent;
      color: var(--accent);
      border: 1px solid var(--accent);
    }
    button.warning { background: var(--accent2); color: #130f05; }
    button.danger { background: var(--danger); color: white; }
    button.emergency {
      min-height: 46px;
      background: #ff1f1f;
      color: white;
      border: 2px solid #ffb4b4;
      box-shadow: 0 0 0 2px rgba(255, 31, 31, .25);
      letter-spacing: .08em;
    }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .buttons { display: grid; gap: 7px; }
    .dimensions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 7px;
      margin: 0 0 10px;
    }
    .dimensions .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 8px;
      background: #0d1112;
      color: var(--muted);
      font-size: 11px;
    }
    .dimensions strong {
      display: block;
      margin-top: 3px;
      color: var(--ink);
      font-size: 13px;
    }
    .stageWrap {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: 44px 1fr;
      background: #141819;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border-bottom: 1px solid var(--line);
      background: #151a1b;
      flex-wrap: wrap;
    }
    .toolchip {
      padding: 6px 9px;
      border: 1px solid var(--line2);
      border-radius: 999px;
      color: var(--muted);
      font-size: 12px;
    }
    .toolchip.warning { color: var(--accent2); border-color: var(--accent2); }
    .toolchip button {
      min-height: 22px;
      padding: 1px 8px;
      font-size: 11px;
      background: transparent;
      color: var(--muted);
      border: 1px solid var(--line2);
      border-radius: 999px;
      font-weight: 600;
    }
    .canvasShell {
      min-width: 0;
      min-height: 0;
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      overflow: hidden;
    }
    .canvasFrame {
      position: relative;
      width: min(82vh, 100%);
      max-width: 820px;
      aspect-ratio: 1;
    }
    canvas {
      width: 100%;
      height: 100%;
      border-radius: 10px;
      border: 1px solid #807966;
      background: var(--field);
      box-shadow: 0 24px 80px rgba(0,0,0,.36);
      cursor: crosshair;
      touch-action: none;
    }
    canvas:active { cursor: crosshair; }
    canvas.panMode { cursor: grab; }
    canvas.panMode:active { cursor: grabbing; }
    canvas.panning { cursor: grabbing; }
    .canvasTools {
      position: absolute;
      top: 10px;
      right: 10px;
      z-index: 5;
      display: flex;
      gap: 5px;
      padding: 5px;
      border: 1px solid rgba(67, 80, 82, .72);
      border-radius: 9px;
      background: rgba(13, 18, 19, .82);
      backdrop-filter: blur(8px);
    }
    .canvasTools button,
    .sidebarToggle {
      min-height: 30px;
      min-width: 34px;
      padding: 3px 8px;
      border-radius: 7px;
      border: 1px solid var(--line2);
      background: rgba(14, 18, 19, .9);
      color: var(--ink);
      font-size: 12px;
      font-weight: 800;
    }
    .canvasTools button.active {
      background: var(--accent);
      color: #061110;
      border-color: var(--accent);
    }
    .sidebarToggle {
      position: absolute;
      top: 12px;
      z-index: 6;
    }
    .sidebarToggle.left { left: 12px; }
    .sidebarToggle.right { right: 12px; }
    .object {
      display: grid;
      gap: 4px;
      padding: 7px 9px;
      border: 1px solid var(--line);
      border-radius: 7px;
      margin-bottom: 6px;
      background: #101516;
      cursor: pointer;
    }
    .object.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
    .object strong { font-size: 13px; }
    .object span { color: var(--muted); font-size: 12px; }
    .object .swatch {
      display: inline-block; width: 9px; height: 9px; border-radius: 50%;
      margin-right: 5px; vertical-align: middle;
    }
    .objectsHeader {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .objectsHeader h2 { margin: 0; }
    .objectCount {
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }
    .objectTools {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 8px;
    }
    .objectTools button {
      min-height: 28px;
      font-size: 12px;
      font-weight: 700;
    }
    #objects {
      max-height: 250px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px;
      background: #0d1112;
    }
    .objectNote {
      color: var(--muted);
      font-size: 11px;
      padding: 7px 3px 2px;
    }
    .layersModule { padding: 0; overflow: hidden; }
    .layersTitle {
      padding: 8px 10px;
      background: linear-gradient(90deg, #2a0b0d, #e41b2d 58%, #f06a11);
      color: #fff;
      font-size: 14px;
      font-weight: 900;
      letter-spacing: .02em;
    }
    .layersBody { display: grid; grid-template-columns: minmax(0, 1fr) 42px; gap: 8px; padding: 8px; }
    .layersTableWrap { border: 1px solid var(--line2); border-radius: 6px; overflow: auto; background: #0b0f10; min-height: 150px; }
    .layersTable { width: 100%; border-collapse: collapse; font-size: 11px; }
    .layersTable th, .layersTable td { padding: 6px 5px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
    .layersTable th { color: var(--muted); background: #151a1b; font-weight: 800; position: sticky; top: 0; }
    .layersTable tr { cursor: pointer; }
    .layersTable tr.active { background: rgba(38, 208, 168, .14); outline: 1px solid var(--accent); }
    .layersTable tr.error td { color: var(--bad); }
    .layersTable input[type=checkbox] { min-height: 0; width: auto; }
    .layersTable .swatch { display: inline-block; width: 13px; height: 13px; border-radius: 3px; vertical-align: middle; border: 1px solid rgba(255,255,255,.35); }
    .layersToolbar { display: grid; gap: 6px; align-content: start; }
    .layersToolbar button { min-height: 34px; padding: 0; }
    .layerInspector { padding: 0 8px 8px; }
    .layerInspectorHeader { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 8px; color: var(--muted); font-size: 11px; }
    .layerInspectorFields { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    .layerInspectorFields label { margin: 0; font-size: 11px; }
    .layerInspectorFields input, .layerInspectorFields select { min-height: 28px; font-size: 12px; padding: 4px 7px; }
    .layerInspector .errs { color: var(--bad); font-size: 11px; margin-top: 7px; }
    .layerTabs { display: flex; padding: 0 8px 8px; }
    .layerTabs span { flex: 1; text-align: center; border: 1px solid var(--line2); border-radius: 7px; padding: 6px 8px; color: var(--ink); background: #141819; font-weight: 800; font-size: 12px; }
    .summary { display: grid; gap: 4px; }
    .summary .row { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; }
    .summary .row span:first-child { color: var(--muted); }
    .summary .row.bad span:last-child { color: var(--bad); font-weight: 700; }
    .summary .row.good span:last-child { color: var(--good); font-weight: 700; }
    pre {
      min-height: 180px;
      max-height: 320px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      border-radius: 7px;
      background: #080b0c;
      color: #d8fff6;
      font: 12px SFMono-Regular, Consolas, monospace;
      line-height: 1.45;
    }
    .kv { display: flex; justify-content: space-between; gap: 8px; padding: 7px 0; border-bottom: 1px solid var(--line); }
    .kv span:first-child { color: var(--muted); }
    .kv:last-child { border-bottom: 0; }
    .kv strong.applied { color: var(--good); }
    .kv strong.unused { color: var(--accent2); }
    .kv strong.notfound { color: var(--bad); }
    .modalScrim {
      position: fixed; inset: 0;
      background: rgba(7, 12, 13, .78);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 30;
    }
    .modalScrim.show { display: flex; }
    .modal {
      width: min(540px, 94vw);
      max-height: 92vh;
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--line2);
      border-radius: 10px;
      padding: 18px;
    }
    .modal h3 { margin: 0 0 12px; font-size: 16px; }
    .modal .errs { color: var(--bad); margin: 8px 0; font-size: 12px; }
    .modal .warns { color: var(--accent2); margin: 8px 0; font-size: 12px; }
    .modal .cta { display: flex; gap: 8px; margin-top: 14px; }
    .contextMenu {
      position: fixed;
      min-width: 190px;
      display: none;
      z-index: 40;
      padding: 6px;
      border: 1px solid var(--line2);
      border-radius: 8px;
      background: #0b0f10;
      box-shadow: 0 18px 54px rgba(0,0,0,.45);
    }
    .contextMenu.show { display: grid; gap: 4px; }
    .contextMenu button {
      min-height: 30px;
      display: flex;
      align-items: center;
      gap: 8px;
      justify-content: flex-start;
      background: transparent;
      color: var(--ink);
      border: 0;
      padding: 5px 7px;
      font-weight: 700;
      text-align: left;
    }
    .contextMenu button:hover { background: #172020; }
    .contextMenu .swatch { width: 10px; height: 10px; border-radius: 3px; }
    details.diagnostics summary {
      color: var(--accent);
      cursor: pointer;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    @media (max-width: 980px) {
      body { overflow: auto; }
      main { height: auto; grid-template-columns: 1fr; }
      aside, .right { border: 0; }
      .stageWrap { min-height: 620px; }
    }
    #connBanner {
      position: fixed; top: -80px; left: 0; right: 0; z-index: 999;
      padding: 12px 20px; text-align: center; font-weight: 700;
      transition: top .25s ease; box-shadow: 0 6px 24px rgba(0,0,0,.45);
    }
    #connBanner.offline { top: 0; background: var(--bad); color: #fff; }
    #connBanner.back { top: 0; background: var(--good); color: #06271c; }
    #connBanner .sub { display: block; font-weight: 400; font-size: 12.5px; opacity: .92; margin-top: 3px; }
    #connBanner .pulse { display: inline-block; animation: connPulse 1.2s ease-in-out infinite; }
    @keyframes connPulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
    body.serverOffline #plan, body.serverOffline #frame, body.serverOffline #frameStart,
    body.serverOffline #frameStop, body.serverOffline #mark, body.serverOffline #dotTest,
    body.serverOffline #lineTest, body.serverOffline #stopEngraving {
      pointer-events: none; opacity: .4;
    }
  </style>
</head>
<body>
  <div id="connBanner" role="alert"></div>
  <header>
    <div class="brandLockup"><h1>OpenLaser</h1><small>motion + light</small></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <div class="machineRail">
        <label for="machineSelect">Machine</label>
        <select id="machineSelect" aria-label="Active machine profile"></select>
      </div>
      <div id="status" class="pill"><span class="dot"></span><span>Checking USB</span></div>
      <div id="powerMode" class="pill">ARM required for emission</div>
    </div>
  </header>
  <main id="appMain">
    <aside class="leftPanel">
      <section>
        <h2>Import</h2>
        <label>File
          <input id="file" type="file" accept=".svg,.dxf,.stl">
        </label>
        <div class="grid2">
          <button id="fit">Fit</button>
          <button class="secondary" id="delete">Delete</button>
        </div>
      </section>
      <section>
        <div class="objectsHeader">
          <h2>Objects</h2>
          <span class="objectCount" id="objectCount">0 objects</span>
        </div>
        <div class="objectTools">
          <button class="secondary" id="selectAllObjects">Select All</button>
          <button class="secondary" id="clearSelectionBtn">Clear</button>
        </div>
        <div id="objects"></div>
      </section>
      <section>
        <h2>Transform</h2>
        <div class="dimensions">
          <div class="metric">Selected Width<strong id="dimWidth">–</strong></div>
          <div class="metric">Selected Height<strong id="dimHeight">–</strong></div>
        </div>
        <div class="grid2">
          <label>X mm<input id="x" type="number" step="0.1" value="0"></label>
          <label>Y mm<input id="y" type="number" step="0.1" value="0"></label>
        </div>
        <div class="grid2">
          <label>Scale<input id="scale" type="number" step="0.01" value="1"></label>
          <label>Rotate deg<input id="rotation" type="number" step="1" value="0"></label>
        </div>
        <div class="grid2">
          <button class="secondary" id="centerSelection">Center Selection</button>
          <button class="secondary" id="centerAll">Center All</button>
        </div>
        <div class="grid2" style="margin-top:8px;">
          <label>Ref X mm<input id="refX" type="number" step="0.1" value="0"></label>
          <label>Ref Y mm<input id="refY" type="number" step="0.1" value="0"></label>
        </div>
        <div class="grid2" style="margin-top:8px;">
          <button class="secondary" id="refCenter">Ref Center</button>
          <button class="secondary" id="refTopLeft">Ref Top Left</button>
        </div>
        <div class="grid2">
          <button class="secondary" id="refTopRight">Ref Top Right</button>
          <button class="secondary" id="refBottomLeft">Ref Bottom Left</button>
        </div>
        <button class="secondary" id="refBottomRight" style="width:100%;margin-top:2px;">Ref Bottom Right</button>
        <button class="secondary" id="moveToRef" style="width:100%;margin-top:2px;">Move Center To Ref</button>
      </section>
      <section>
        <h2>Edit</h2>
        <label>Join tolerance mm
          <input id="joinTolerance" type="number" min="0.001" step="0.005" value="0.05">
        </label>
        <div class="grid2" style="margin-bottom:8px;">
          <button class="secondary" id="groupSelection">Group Selected</button>
          <button class="secondary" id="ungroupSelection">Ungroup</button>
        </div>
        <button class="secondary" id="joinLines" style="width:100%;">Join Connected Lines</button>
      </section>
      <section>
        <details class="diagnostics">
          <summary>Machine Profile</summary>
          <div id="config" style="margin-top:10px;"></div>
          <button class="secondary" id="inspectProfile" style="margin-top:8px;width:100%;">Inspect Profile Usage</button>
        </details>
      </section>
    </aside>

    <div class="stageWrap">
      <div class="toolbar">
        <div class="toolchip">
          <button id="undoBtn">Undo</button>
          <button id="redoBtn">Redo</button>
        </div>
        <div class="toolchip" id="fieldChip">Field 200 mm</div>
        <div class="toolchip" id="selection">No selection</div>
        <div class="toolchip" id="jobStats">0 paths</div>
        <div class="toolchip" id="zoomChip">100%
          <button id="zoomFit">Fit</button>
          <button id="zoomReset">100%</button>
        </div>
        <div class="toolchip" id="cursorChip">x= 0.0 y= 0.0 mm</div>
      </div>
      <div class="canvasShell">
        <button class="sidebarToggle left" id="toggleLeft" title="Collapse left sidebar">‹</button>
        <button class="sidebarToggle right" id="toggleRight" title="Collapse right sidebar">›</button>
        <div class="canvasFrame">
          <div class="canvasTools">
            <button id="selectTool" class="active" title="Select vectors">Cursor</button>
            <button id="panTool" title="Pan canvas">Pan</button>
          </div>
          <canvas id="stage" width="900" height="900"></canvas>
        </div>
      </div>
    </div>

    <div class="right rightPanel">
      <section class="layersModule">
        <div class="layersTitle">Cuts / Layers</div>
        <div id="layers"></div>
      </section>
      <section>
        <h2>Job Controls</h2>
        <label id="armControl">ARM<input id="confirm" type="text" placeholder="Type ARM exactly"></label>
        <div class="grid2">
          <label><input id="selectedOnly" type="checkbox"> Selected only</label>
          <label>Repeat<input id="markRepeat" type="number" min="1" max="999" step="1" value="1"></label>
        </div>
        <div class="grid2">
          <label><input id="mirrorX" type="checkbox"> Mirror horizontally</label>
          <label><input id="mirrorY" type="checkbox"> Mirror vertically</label>
        </div>
        <div class="buttons">
          <button id="plan">Plan Job</button>
          <button class="secondary" id="exportGcode">Export G-code</button>
          <button class="secondary" id="frame">Frame Once</button>
          <button class="secondary" id="frameStart">Start Continuous Frame</button>
          <button class="secondary" id="frameStop">Stop Frame</button>
          <button class="emergency" id="stopEngraving">STOP ENGRAVING</button>
          <button class="danger" id="mark">Mark Job</button>
        </div>
      </section>
      <section id="fiberAdvanced">
        <details>
          <summary><h2>Advanced</h2></summary>
          <div class="info">
            <strong>Scale calibration</strong>
            Corrects mark size when the engraved result differs from the design.
            Set this to (measured &divide; intended) from a test square &mdash; e.g. a
            50 mm square that comes out 59.17 mm &rarr; 1.1834. Applied to every job;
            the canvas &ldquo;Field&rdquo; readout shows the corrected work area.
          </div>
          <div class="grid2">
            <label>Scale correction<input id="scaleCorrection" type="number" min="0.5" max="2" step="0.0001" value="1"></label>
          </div>
          <div class="info">
            <strong>Legacy frame parameters</strong>
            These values are only for red-light framing / planning defaults, not for normal layer marking.
            Marking uses the selected layer's power, frequency, pulse, speed, and passes from Cuts / Layers.
          </div>
          <div class="grid2">
            <label>Frame Power %<input id="power" type="number" min="0" max="100" step="1" value="100"></label>
            <label>Frame Frequency kHz<input id="frequency" type="number" min="1" max="4000" step="1" value="30"></label>
          </div>
          <div class="grid2">
            <label>Frame Pulse ns<select id="pulseWidth"></select></label>
            <label>Frame Speed mm/s<input id="markSpeed" type="number" min="10" max="6000" step="10" value="1000"></label>
          </div>
          <div class="info">
            <strong>First-burn helpers</strong>
            Diagnostic test-fire shortcuts for setup only. Test Dot confirms emission at one spot.
            5 mm Line confirms emission plus galvo motion. They are not part of the normal job workflow.
          </div>
          <div class="grid2">
            <button class="warning" id="dotTest">Test Dot</button>
            <button class="warning" id="lineTest">5 mm Line</button>
          </div>
        </details>
      </section>
      <section>
        <h2>Activity</h2>
        <pre id="output">Ready.</pre>
      </section>
      <section>
        <h2>Job Summary</h2>
        <div id="summary" class="summary"></div>
      </section>
    </div>
  </main>

  <div id="modalScrim" class="modalScrim">
    <div class="modal">
      <h3 id="modalTitle">Confirm marking</h3>
      <div id="modalBody"></div>
      <div class="cta">
        <button class="danger" id="modalConfirm">Confirm and Mark</button>
        <button class="secondary" id="modalCancel">Cancel</button>
      </div>
    </div>
  </div>
  <div id="layerMenu" class="contextMenu"></div>

  <script>
    let FIELD_W_MM = 200;
    let FIELD_H_MM = 200;
    let fieldNominalMm = 200;
    let machines = [];
    let activeMachine = null;
    const PULSES = [2,4,6,9,13,20,30,45,55,60,80,100,150,200,250,300,350,400,450,500];
    const objects = [];
    let layers = [];
    let activeLayerId = null;
    let selected = new Set();
    let lastSelected = -1;
    let dragging = null;
    let marquee = null;
    let panning = null;
    let spaceHeld = false;
    let lastRasterLineCount = 0;
    let zoom = 1;
    let viewX = 0;
    let viewY = 0;
    let toolMode = 'select';
    let safety = {};
    let undoStack = [];
    let redoStack = [];
    let transformEditActive = false;
    let transformEditBaseline = null;
    let workspaceSaveTimer = null;
    let lastWorkspaceJson = null;
    let workspaceReady = false;
    const MAX_HISTORY = 30;
    const canvas = document.getElementById('stage');
    const ctx = canvas.getContext('2d');
    const out = document.getElementById('output');
    const pulseSelect = document.getElementById('pulseWidth');
    PULSES.forEach(value => {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = `${value} ns`;
      if (value === 200) option.selected = true;
      pulseSelect.appendChild(option);
    });

    function show(value) {
      if (typeof value === 'string') {
        out.textContent = value;
      } else if (value instanceof Error) {
        out.textContent = `${value.name}: ${value.message}`;
      } else {
        out.textContent = JSON.stringify(value, null, 2);
      }
    }
    function clone(value) {
      return JSON.parse(JSON.stringify(value));
    }
    function snapshotState() {
      return {
        objects: clone(objects),
        layers: clone(layers),
        activeLayerId,
        selected: selectionList(),
        lastSelected,
      };
    }
    function restoreState(state) {
      objects.splice(0, objects.length, ...clone(state.objects || []));
      layers = clone(state.layers || []);
      activeLayerId = state.activeLayerId || (layers[0] && layers[0].layer_id) || null;
      selected = new Set(state.selected || []);
      lastSelected = Number.isFinite(state.lastSelected) ? state.lastSelected : -1;
      render();
    }
    function recordHistory() {
      undoStack.push(snapshotState());
      if (undoStack.length > MAX_HISTORY) undoStack.shift();
      redoStack = [];
      updateUndoRedoButtons();
    }
    function undo() {
      if (!undoStack.length) return;
      redoStack.push(snapshotState());
      restoreState(undoStack.pop());
      updateUndoRedoButtons();
    }
    function redo() {
      if (!redoStack.length) return;
      undoStack.push(snapshotState());
      restoreState(redoStack.pop());
      updateUndoRedoButtons();
    }
    function updateUndoRedoButtons() {
      const undoBtn = document.getElementById('undoBtn');
      const redoBtn = document.getElementById('redoBtn');
      if (!undoBtn || !redoBtn) return;
      undoBtn.disabled = undoStack.length === 0;
      redoBtn.disabled = redoStack.length === 0;
    }
    function setToolMode(mode) {
      toolMode = mode === 'pan' ? 'pan' : 'select';
      document.getElementById('selectTool').classList.toggle('active', toolMode === 'select');
      document.getElementById('panTool').classList.toggle('active', toolMode === 'pan');
      canvas.classList.toggle('panMode', toolMode === 'pan');
    }
    function toggleSidebar(side) {
      const main = document.getElementById('appMain');
      const isLeft = side === 'left';
      const className = isLeft ? 'leftCollapsed' : 'rightCollapsed';
      main.classList.toggle(className);
      document.getElementById(isLeft ? 'toggleLeft' : 'toggleRight').textContent =
        main.classList.contains(className)
          ? (isLeft ? '›' : '‹')
          : (isLeft ? '‹' : '›');
    }
    async function getJson(url, options) {
      const response = await fetch(url, options);
      const text = await response.text();
      let data;
      try {
        data = text ? JSON.parse(text) : {};
      } catch (error) {
        data = { error: text || `${response.status} ${response.statusText}` };
      }
      if (!response.ok) throw data;
      return data;
    }
    function pixelsPerMm() {
      return Math.min(canvas.width / FIELD_W_MM, canvas.height / FIELD_H_MM) * zoom;
    }
    function mmToPx(x, y) {
      const s = pixelsPerMm();
      return [canvas.width / 2 + (x - viewX) * s, canvas.height / 2 - (y - viewY) * s];
    }
    function pxToMm(px, py) {
      const s = pixelsPerMm();
      return [(px - canvas.width / 2) / s + viewX, -(py - canvas.height / 2) / s + viewY];
    }
    function parseNumericInput(id, fallback = 0) {
      const value = String(document.getElementById(id).value).trim().replace(',', '.');
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }
    function transformPoint(obj, pt) {
      const angle = obj.rotation * Math.PI / 180;
      const x = pt[0] * obj.scale;
      const y = pt[1] * obj.scale;
      return [
        x * Math.cos(angle) - y * Math.sin(angle) + obj.x,
        x * Math.sin(angle) + y * Math.cos(angle) + obj.y
      ];
    }
    function objectTransformedPolylines(obj) {
      return obj.polylines.map(poly => poly.map(pt => transformPoint(obj, pt)));
    }
    function transformedPolylines() {
      return objects.flatMap(obj => objectTransformedPolylines(obj));
    }
    function objectsForLayer(layerId) {
      return objects.filter(obj => obj.layer_id === layerId);
    }
    function layerById(id) { return layers.find(layer => layer.layer_id === id); }
    function isSelected(index) { return selected.has(index); }
    function resetTransformEdit() { transformEditActive = false; transformEditBaseline = null; }
    function clearSelection() { selected.clear(); lastSelected = -1; resetTransformEdit(); }
    function selectOne(index) { selected = new Set([index]); lastSelected = index; resetTransformEdit(); }
    function toggleSelected(index) {
      if (selected.has(index)) selected.delete(index); else selected.add(index);
      lastSelected = index;
      resetTransformEdit();
    }
    function selectionList() {
      return [...selected].sort((a, b) => a - b).filter(i => i >= 0 && i < objects.length);
    }
    function drawGrid() {
      const halfW = FIELD_W_MM / 2;
      const halfH = FIELD_H_MM / 2;
      ctx.fillStyle = '#141819';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      const [fieldLeft, fieldBottom] = mmToPx(-halfW, -halfH);
      const [fieldRight, fieldTop] = mmToPx(halfW, halfH);
      ctx.fillStyle = '#d8d0be';
      ctx.fillRect(fieldLeft, fieldTop, fieldRight - fieldLeft, fieldBottom - fieldTop);

      // Pick a tick interval in mm based on current zoom so labels never crowd.
      const ppm = pixelsPerMm();
      const candidates = [1, 2, 5, 10, 20, 50, 100];
      let majorMm = 10;
      for (const c of candidates) {
        if (c * ppm >= 56) { majorMm = c; break; }
      }
      const minorMm = majorMm / (majorMm % 5 === 0 ? 5 : 2);

      // Minor grid.
      ctx.strokeStyle = 'rgba(31,42,43,.10)';
      ctx.lineWidth = 1;
      for (let mm = Math.ceil(-halfW / minorMm) * minorMm; mm <= halfW; mm += minorMm) {
        if (Math.abs(mm % majorMm) < 1e-6) continue;
        const [x0, y0] = mmToPx(mm, -halfH);
        const [x1, y1] = mmToPx(mm, halfH);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      }
      for (let mm = Math.ceil(-halfH / minorMm) * minorMm; mm <= halfH; mm += minorMm) {
        if (Math.abs(mm % majorMm) < 1e-6) continue;
        const [x2, y2] = mmToPx(-halfW, mm);
        const [x3, y3] = mmToPx(halfW, mm);
        ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x3, y3); ctx.stroke();
      }

      // Major grid.
      ctx.strokeStyle = 'rgba(31,42,43,.22)';
      for (let mm = Math.ceil(-halfW / majorMm) * majorMm; mm <= halfW; mm += majorMm) {
        const [x0, y0] = mmToPx(mm, -halfH);
        const [x1, y1] = mmToPx(mm, halfH);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      }
      for (let mm = Math.ceil(-halfH / majorMm) * majorMm; mm <= halfH; mm += majorMm) {
        const [x2, y2] = mmToPx(-halfW, mm);
        const [x3, y3] = mmToPx(halfW, mm);
        ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x3, y3); ctx.stroke();
      }

      // Field rectangle and crosshair.
      ctx.strokeStyle = '#1c2a2b';
      ctx.lineWidth = 2;
      const [bx0, by0] = mmToPx(-halfW, -halfH);
      const [bx1, by1] = mmToPx(halfW, halfH);
      ctx.strokeRect(bx0, by1, bx1 - bx0, by0 - by1);
      const [cx0, cy0] = mmToPx(-halfW, 0);
      const [cx1, cy1] = mmToPx(halfW, 0);
      const [cx2, cy2] = mmToPx(0, -halfH);
      const [cx3, cy3] = mmToPx(0, halfH);
      ctx.strokeStyle = 'rgba(11,28,29,.28)';
      ctx.beginPath(); ctx.moveTo(cx0, cy0); ctx.lineTo(cx1, cy1); ctx.moveTo(cx2, cy2); ctx.lineTo(cx3, cy3); ctx.stroke();

      // Ruler: mm labels along bottom and left edges of the field.
      ctx.fillStyle = '#1c2a2b';
      ctx.font = '600 11px SFMono-Regular, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const padX = 4;
      const padY = 4;
      for (let mm = Math.ceil(-halfW / majorMm) * majorMm; mm <= halfW; mm += majorMm) {
        const [x, y] = mmToPx(mm, -halfH);
        if (x < 18 || x > canvas.width - 18) continue;
        // Tick mark.
        ctx.strokeStyle = '#1c2a2b';
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y + 6); ctx.stroke();
        ctx.fillText(mm.toString(), x, y + 8);
      }
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (let mm = Math.ceil(-halfH / majorMm) * majorMm; mm <= halfH; mm += majorMm) {
        const [x, y] = mmToPx(-halfW, mm);
        if (y < 14 || y > canvas.height - 14) continue;
        ctx.strokeStyle = '#1c2a2b';
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 6, y); ctx.stroke();
        ctx.fillText(mm.toString(), x - 8, y);
      }

      // Scale bar: a labelled segment in the lower-right showing one major step.
      const barLengthMm = majorMm;
      const barLengthPx = barLengthMm * ppm;
      const barX1 = canvas.width - 24;
      const barX0 = barX1 - barLengthPx;
      const barY = canvas.height - 28;
      ctx.lineWidth = 3;
      ctx.strokeStyle = '#1c2a2b';
      ctx.beginPath(); ctx.moveTo(barX0, barY); ctx.lineTo(barX1, barY); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(barX0, barY - 5); ctx.lineTo(barX0, barY + 5);
      ctx.moveTo(barX1, barY - 5); ctx.lineTo(barX1, barY + 5);
      ctx.stroke();
      ctx.fillStyle = '#1c2a2b';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText(`${barLengthMm} mm`, (barX0 + barX1) / 2, barY - 7);
    }
    function drawObjects() {
      objects.forEach((obj, index) => {
        const layer = layerById(obj.layer_id);
        const visible = !layer || layer.visible !== false;
        if (!visible) return;
        const baseColor = (layer && layer.color) || '#0c2625';
        ctx.strokeStyle = isSelected(index) ? '#26d0a8' : baseColor;
        ctx.lineWidth = isSelected(index) ? 2.4 : 1.4;
        for (const poly of obj.polylines) {
          if (poly.length < 2) continue;
          ctx.beginPath();
          poly.forEach((pt, i) => {
            const [x, y] = mmToPx(...transformPoint(obj, pt));
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.stroke();
        }
      });
    }
    function isClosedPoly(poly, tol) {
      if (!poly || poly.length < 3) return false;
      const a = poly[0], b = poly[poly.length - 1];
      return Math.hypot(a[0] - b[0], a[1] - b[1]) <= tol;
    }
    function hatchOnePassJS(closed, angleDeg, pitch) {
      const ri = -angleDeg * Math.PI / 180;
      const ro =  angleDeg * Math.PI / 180;
      const ci = Math.cos(ri), si = Math.sin(ri);
      const co = Math.cos(ro), so = Math.sin(ro);
      const rotated = closed.map(poly => poly.map(([x, y]) => [x*ci - y*si, x*si + y*ci]));
      let minY = Infinity, maxY = -Infinity;
      for (const poly of rotated) for (const pt of poly) {
        if (pt[1] < minY) minY = pt[1];
        if (pt[1] > maxY) maxY = pt[1];
      }
      if (!isFinite(minY)) return [];
      const offset = pitch * 0.0173;
      const yStart = Math.floor(minY / pitch) * pitch + offset;
      const segments = [];
      let flip = false;
      for (let y = yStart; y < maxY + pitch; y += pitch) {
        const xs = [];
        for (const poly of rotated) {
          for (let i = 0; i < poly.length - 1; i++) {
            const ay = poly[i][1], by = poly[i+1][1];
            if (ay === by) continue;
            const lo = Math.min(ay, by), hi = Math.max(ay, by);
            if (y < lo || y >= hi) continue;
            const t = (y - ay) / (by - ay);
            xs.push(poly[i][0] + t * (poly[i+1][0] - poly[i][0]));
          }
        }
        xs.sort((a, b) => a - b);
        const pairs = [];
        for (let i = 0; i + 1 < xs.length; i += 2) {
          if (xs[i+1] - xs[i] > 1e-6) pairs.push([xs[i], xs[i+1]]);
        }
        if (flip) pairs.reverse();
        for (let [x0, x1] of pairs) {
          if (flip) { const t = x0; x0 = x1; x1 = t; }
          segments.push([
            [x0*co - y*so, x0*so + y*co],
            [x1*co - y*so, x1*so + y*co],
          ]);
        }
        flip = !flip;
      }
      return segments;
    }
    function hatchPolylinesJS(polylines, angleDeg, pitchMm, passes, angleStepDeg) {
      if (pitchMm <= 0 || passes < 1) return [];
      const tol = Math.max(0.05, pitchMm * 0.5);
      const closed = polylines.filter(p => isClosedPoly(p, tol));
      if (!closed.length) return [];
      const out = [];
      for (let p = 0; p < passes; p++) {
        const a = angleDeg + p * angleStepDeg;
        out.push(...hatchOnePassJS(closed, a, pitchMm));
        if (out.length > 12000) break;  // safety cap on the preview
      }
      return out;
    }
    function rasterPolylinesForLayer(layer) {
      const polys = [];
      objects.forEach(obj => {
        if (obj.layer_id !== layer.layer_id) return;
        for (const poly of objectTransformedPolylines(obj)) polys.push(poly);
      });
      return polys;
    }
    function drawRasterPreviews() {
      let totalLines = 0;
      layers.forEach(layer => {
        if (layer.operation !== 'raster_engrave') return;
        if (layer.visible === false) return;
        if (layer.output === false) return;
        const polys = rasterPolylinesForLayer(layer);
        if (!polys.length) return;
        const segs = hatchPolylinesJS(
          polys,
          Number(layer.raster_angle_deg ?? 0),
          Number(layer.raster_pitch_mm ?? 0.1),
          Math.max(1, Number(layer.passes ?? 1)),
          Number(layer.raster_angle_step_deg ?? 90),
        );
        totalLines += segs.length;
        if (!segs.length) return;
        ctx.save();
        ctx.strokeStyle = layer.color || '#7aa2ff';
        ctx.globalAlpha = 0.4;
        ctx.lineWidth = 0.9;
        ctx.beginPath();
        for (const seg of segs) {
          const [x0, y0] = mmToPx(seg[0][0], seg[0][1]);
          const [x1, y1] = mmToPx(seg[1][0], seg[1][1]);
          ctx.moveTo(x0, y0); ctx.lineTo(x1, y1);
        }
        ctx.stroke();
        ctx.restore();
      });
      lastRasterLineCount = totalLines;
    }
    function drawMarquee() {
      if (!marquee) return;
      const [x0, y0] = mmToPx(...marquee.startMm);
      const [x1, y1] = mmToPx(...marquee.endMm);
      const left = Math.min(x0, x1);
      const top = Math.min(y0, y1);
      const width = Math.abs(x1 - x0);
      const height = Math.abs(y1 - y0);
      ctx.save();
      ctx.fillStyle = 'rgba(38, 208, 168, .12)';
      ctx.strokeStyle = '#26d0a8';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([8, 5]);
      ctx.fillRect(left, top, width, height);
      ctx.strokeRect(left, top, width, height);
      ctx.restore();
    }
    function render() {
      drawGrid();
      drawObjects();
      drawRasterPreviews();
      drawMarquee();
      renderObjects();
      renderLayers();
      syncControls();
      updateZoomChip();
      updateSummary();
      updateUndoRedoButtons();
      scheduleWorkspaceSave();
    }
    function renderObjects() {
      const list = document.getElementById('objects');
      list.innerHTML = '';
      const count = document.getElementById('objectCount');
      const sel = selectionList();
      if (count) count.textContent = `${objects.length} objects · ${sel.length} selected`;
      const visibleIndexes = new Set();
      for (let i = 0; i < Math.min(objects.length, 180); i++) visibleIndexes.add(i);
      if (sel.length <= 80) sel.forEach(i => visibleIndexes.add(i));
      [...visibleIndexes].sort((a, b) => a - b).forEach(index => {
        const obj = objects[index];
        const layer = layerById(obj.layer_id);
        const swatch = layer ? `<span class="swatch" style="background:${layer.color}"></span>` : '';
        const item = document.createElement('div');
        item.className = `object ${isSelected(index) ? 'active' : ''}`;
        item.innerHTML = `<strong>${swatch}${index + 1}. ${obj.name}</strong>` +
          `<span>${obj.polylines.length} paths / ${obj.pointCount} pts · ${(layer && layer.name) || obj.layer_id}</span>`;
        item.addEventListener('click', event => {
          if (event.shiftKey) toggleSelected(index); else selectOne(index);
          render();
        });
        list.appendChild(item);
      });
      if (objects.length > visibleIndexes.size) {
        const note = document.createElement('div');
        note.className = 'objectNote';
        note.textContent = `${objects.length - visibleIndexes.size} more hidden here. Use canvas window-select, double-click connected selection, or Select All.`;
        list.appendChild(note);
      }
    }
    async function saveLayers() {
      try {
        await postJson('/api/layers/save', { layers });
      } catch (error) {
        show(error);
      }
    }
    function moveActiveLayer(delta) {
      const index = layers.findIndex(layer => layer.layer_id === activeLayerId);
      const next = index + delta;
      if (index < 0 || next < 0 || next >= layers.length) return;
      recordHistory();
      const [layer] = layers.splice(index, 1);
      layers.splice(next, 0, layer);
      saveLayers();
      render();
    }
    async function resetLayersToDefaults() {
      const data = await getJson('/api/layers/defaults');
      recordHistory();
      layers = data.layers;
      activeLayerId = layers[0].layer_id;
      objects.forEach(obj => { if (!layerById(obj.layer_id)) obj.layer_id = activeLayerId; });
      await saveLayers();
      render();
    }
    function renderLayers() {
      const list = document.getElementById('layers');
      const active = layerById(activeLayerId) || layers[0];
      if (active) activeLayerId = active.layer_id;
      const activeIndex = layers.findIndex(layer => layer.layer_id === activeLayerId);
      const activeErrors = active ? (active._errors || []) : [];
      list.innerHTML = `
        <div class="layersBody">
          <div class="layersTableWrap">
            <table class="layersTable">
              <thead><tr><th>#</th><th></th><th>Layer</th><th>Mode</th><th>Spd/Pwr</th><th>Output</th><th>Show</th></tr></thead>
              <tbody>
                ${layers.map((layer, index) => {
                  const errs = (layer._errors || []).length;
                  const count = objectsForLayer(layer.layer_id).length;
                  return `<tr data-layer-id="${layer.layer_id}" class="${layer.layer_id === activeLayerId ? 'active' : ''} ${errs ? 'error' : ''}">
                    <td>${index + 1}</td>
                    <td><span class="swatch" style="background:${layer.color}"></span></td>
                    <td>${layer.name} <span style="color:var(--muted)">(${count})</span></td>
                    <td>${layer.operation.replace(/_/g, ' ')}</td>
                    <td>${Number(layer.speed_mm_s).toFixed(0)} / ${Number(layer.power_percent).toFixed(1)}%</td>
                    <td><input data-row-key="output" data-layer-id="${layer.layer_id}" type="checkbox" ${layer.output ? 'checked' : ''}></td>
                    <td><input data-row-key="visible" data-layer-id="${layer.layer_id}" type="checkbox" ${layer.visible ? 'checked' : ''}></td>
                  </tr>`;
                }).join('')}
              </tbody>
            </table>
          </div>
          <div class="layersToolbar">
            <button class="secondary" id="moveLayerUp" title="Move layer up">▲</button>
            <button class="secondary" id="moveLayerDown" title="Move layer down">▼</button>
            <button class="secondary" id="assignLayer" title="Move selected vectors to selected layer">→</button>
            <button class="secondary" id="resetLayers" title="Reset default layers">Reset</button>
          </div>
        </div>
        <div class="layerInspector">
          ${active ? `
            <div class="layerInspectorHeader">
              <strong>Selected: ${active.name}</strong>
              <span>${activeErrors.length ? activeErrors.length + ' error(s)' : 'saved to layer_settings.json'}</span>
            </div>
            <div class="layerInspectorFields">
              <label>Layer Color<input data-key="color" type="color" value="${active.color}"></label>
              <label>Name<input data-key="name" type="text" value="${active.name}"></label>
              <label>Mode<select data-key="operation">
                ${['vector_engrave','vector_cut','raster_engrave','frame_only','disabled'].map(op => `<option value="${op}" ${op === active.operation ? 'selected':''}>${op}</option>`).join('')}
              </select></label>
              <label>Speed mm/s<input data-key="speed_mm_s" type="number" min="1" step="10" value="${active.speed_mm_s}"></label>
              ${!activeMachine || activeMachine.is_laser ? `<label>Power %<input data-key="power_percent" type="number" min="0" max="100" step="0.1" value="${active.power_percent}"></label>` : ''}
              ${activeMachine && activeMachine.uses_frequency ? `<label>Frequency kHz<input data-key="frequency_khz" type="number" min="1" step="1" value="${active.frequency_khz}"></label>` : ''}
              ${activeMachine && activeMachine.uses_pulse_width ? `<label>Pulse ns<select data-key="pulse_width_ns">${PULSES.map(p => `<option value="${p}" ${p == active.pulse_width_ns ? 'selected':''}>${p}</option>`).join('')}</select></label>` : ''}
              ${activeMachine && activeMachine.supports_air_assist ? `<label><input data-key="air_assist" type="checkbox" ${active.air_assist ? 'checked' : ''}> Air assist (M8/M9)</label>` : ''}
              <label>Passes<input data-key="passes" type="number" min="1" step="1" value="${active.passes}"></label>
              ${active.operation === 'raster_engrave' ? `
                <label>Hatch angle deg<input data-key="raster_angle_deg" type="number" step="1" value="${active.raster_angle_deg ?? 0}"></label>
                <label>Pitch mm<input data-key="raster_pitch_mm" type="number" min="0.01" step="0.01" value="${active.raster_pitch_mm ?? 0.1}"></label>
                <label>Angle step / pass<input data-key="raster_angle_step_deg" type="number" step="1" value="${active.raster_angle_step_deg ?? 90}"></label>
              ` : ''}
            </div>
            ${activeErrors.length ? `<div class="errs">${activeErrors.join('; ')}</div>` : ''}
          ` : '<div class="layerInspectorHeader">No layers</div>'}
        </div>
        <div class="layerTabs"><span>Cuts / Layers</span></div>
      `;
      list.querySelectorAll('tbody tr').forEach(row => {
        row.addEventListener('click', event => {
          if (event.target.tagName === 'INPUT') return;
          activeLayerId = row.dataset.layerId;
          render();
        });
      });
      list.querySelectorAll('[data-row-key]').forEach(field => {
        field.addEventListener('change', event => {
          event.stopPropagation();
          const layer = layerById(field.dataset.layerId);
          if (!layer) return;
          recordHistory();
          layer[field.dataset.rowKey] = field.checked;
          saveLayers();
          render();
        });
      });
      list.querySelectorAll('.layerInspector [data-key]').forEach(field => {
        field.addEventListener('change', () => {
          if (!active) return;
          recordHistory();
          const key = field.dataset.key;
          let value = field.value;
          if (field.type === 'checkbox') value = field.checked;
          else if (key === 'operation' || key === 'name' || key === 'color') value = String(value);
          else value = Number(value);
          active[key] = value;
          saveLayers();
          render();
        });
      });
      document.getElementById('assignLayer').disabled = selectionList().length === 0 || !active;
      document.getElementById('assignLayer').addEventListener('click', () => assignSelectionToLayer(activeLayerId));
      document.getElementById('moveLayerUp').disabled = activeIndex <= 0;
      document.getElementById('moveLayerDown').disabled = activeIndex < 0 || activeIndex >= layers.length - 1;
      document.getElementById('moveLayerUp').addEventListener('click', () => moveActiveLayer(-1));
      document.getElementById('moveLayerDown').addEventListener('click', () => moveActiveLayer(1));
      document.getElementById('resetLayers').addEventListener('click', resetLayersToDefaults);
    }
    function syncControls() {
      const sel = selectionList();
      const obj = sel.length === 1 ? objects[sel[0]] : null;
      const box = sel.length ? bboxForIndices(sel) : null;
      document.getElementById('selection').textContent = sel.length === 0
        ? 'No selection'
        : sel.length === 1 ? obj.name : `${sel.length} objects`;
      ['x','y','scale','rotation'].forEach(id => document.getElementById(id).disabled = !box);
      document.getElementById('dimWidth').textContent = box ? `${box.width.toFixed(2)} mm` : '–';
      document.getElementById('dimHeight').textContent = box ? `${box.height.toFixed(2)} mm` : '–';
      if (!box) return;
      if (transformEditActive) return;
      document.getElementById('x').value = obj ? obj.x.toFixed(2) : box.cx.toFixed(2);
      document.getElementById('y').value = obj ? obj.y.toFixed(2) : box.cy.toFixed(2);
      document.getElementById('scale').value = obj ? obj.scale.toFixed(3) : '1.000';
      document.getElementById('rotation').value = obj ? obj.rotation.toFixed(1) : '0.0';
    }
    function cloneObject(obj) {
      return {
        ...obj,
        polylines: obj.polylines.map(polyline => polyline.map(point => [point[0], point[1]])),
      };
    }
    function beginTransformEdit(sel) {
      if (!transformEditActive) {
        recordHistory();
        transformEditActive = true;
        transformEditBaseline = {
          indices: [...sel],
          objects: sel.map(i => cloneObject(objects[i])),
          box: bboxForIndices(sel),
        };
      }
      return transformEditBaseline;
    }
    function applyMultiTransform(baseline, targetX, targetY, scale, rotation) {
      if (!baseline || !baseline.box) return;
      const angle = rotation * Math.PI / 180;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      baseline.indices.forEach((objectIndex, baselineIndex) => {
        const baseObj = baseline.objects[baselineIndex];
        const transformed = objectTransformedPolylines(baseObj).map(polyline => polyline.map(point => {
          const dx = (point[0] - baseline.box.cx) * scale;
          const dy = (point[1] - baseline.box.cy) * scale;
          return [
            dx * cos - dy * sin + targetX,
            dx * sin + dy * cos + targetY,
          ];
        }));
        objects[objectIndex].polylines = transformed;
        objects[objectIndex].pointCount = transformed.reduce((acc, polyline) => acc + polyline.length, 0);
        objects[objectIndex].x = 0;
        objects[objectIndex].y = 0;
        objects[objectIndex].scale = 1;
        objects[objectIndex].rotation = 0;
      });
    }
    function updateSelected() {
      const sel = selectionList();
      if (!sel.length) return;
      const baseline = beginTransformEdit(sel);
      if (sel.length === 1) {
        const obj = objects[sel[0]];
        obj.x = parseNumericInput('x', obj.x);
        obj.y = parseNumericInput('y', obj.y);
        obj.scale = parseNumericInput('scale', obj.scale);
        obj.rotation = parseNumericInput('rotation', obj.rotation);
      } else {
        applyMultiTransform(
          baseline,
          parseNumericInput('x', baseline.box.cx),
          parseNumericInput('y', baseline.box.cy),
          parseNumericInput('scale', 1),
          parseNumericInput('rotation', 0),
        );
      }
      render();
    }
    function updateZoomChip() {
      document.getElementById('zoomChip').firstChild.nodeValue = `${Math.round(zoom * 100)}%`;
    }
    function updateSummary() {
      validateLayersLocally();
      const summary = document.getElementById('summary');
      const sel = selectionList();
      const polylines = transformedPolylines();
      const allPoints = polylines.flat();
      let bbox = '—';
      if (allPoints.length) {
        const xs = allPoints.map(p => p[0]);
        const ys = allPoints.map(p => p[1]);
        const w = Math.max(...xs) - Math.min(...xs);
        const h = Math.max(...ys) - Math.min(...ys);
        bbox = `${w.toFixed(2)} × ${h.toFixed(2)} mm`;
      }
      const layerStats = layers.map(layer => {
        const objs = objectsForLayer(layer.layer_id);
        const pCount = objs.reduce((acc, o) => acc + o.polylines.length, 0);
        return { layer, objs: objs.length, paths: pCount };
      });
      const armText = document.getElementById('confirm').value.trim().toUpperCase();
      const armOk = armText === 'ARM';
      const power = Number(document.getElementById('power').value);
      const freq = Number(document.getElementById('frequency').value);
      const pulse = Number(document.getElementById('pulseWidth').value);
      const speed = Number(document.getElementById('markSpeed').value);
      const layerErrors = layers.flatMap(l => (l._errors || []).map(e => `${l.name}: ${e}`));
      const emittingLayers = layers.filter(l => l.output && !['disabled','frame_only'].includes(l.operation));
      const rasterLayers = layers.filter(l => l.output && l.visible !== false && l.operation === 'raster_engrave');
      const willEmit = armOk && (polylines.length > 0 || lastRasterLineCount > 0) && emittingLayers.length > 0;
      summary.innerHTML = `
        <div class="row"><span>Objects</span><span>${objects.length}</span></div>
        <div class="row"><span>Selected</span><span>${sel.length}</span></div>
        <div class="row"><span>Layers</span><span>${layers.length}</span></div>
        <div class="row"><span>Paths</span><span>${polylines.length}</span></div>
        <div class="row"><span>BBox</span><span>${bbox}</span></div>
        ${rasterLayers.length ? `<div class="row"><span>Raster lines</span><span>${lastRasterLineCount}</span></div>` : ''}
        <div class="row"><span>Power %</span><span>${power}</span></div>
        <div class="row"><span>Freq kHz</span><span>${freq}</span></div>
        <div class="row"><span>Pulse ns</span><span>${pulse}</span></div>
        <div class="row"><span>Speed mm/s</span><span>${speed}</span></div>
        <div class="row ${armOk ? 'good':'bad'}"><span>ARM</span><span>${armOk ? 'typed' : 'missing'}</span></div>
        <div class="row ${willEmit ? 'bad':'good'}"><span>Will emit?</span><span>${willEmit ? 'YES' : 'no'}</span></div>
        ${layerStats.map(s => `<div class="row"><span>· ${s.layer.name}</span><span>${s.objs} obj / ${s.paths} paths · ${s.layer.operation}</span></div>`).join('')}
        ${layerErrors.length ? `<div class="row bad"><span>Layer errors</span><span>${layerErrors.length}</span></div>` : ''}
      `;
      document.getElementById('mark').disabled =
        !armOk || (polylines.length === 0 && lastRasterLineCount === 0) || layerErrors.length > 0;
    }
    function validateLayersLocally() {
      layers.forEach(layer => {
        const errs = [];
        if (layer.power_percent < 0 || layer.power_percent > 100) errs.push('power 0..100');
        if (layer.speed_mm_s <= 0) errs.push('speed > 0');
        if (activeMachine && activeMachine.uses_pulse_width && !PULSES.includes(Number(layer.pulse_width_ns))) errs.push('pulse not in JPT table');
        if (layer.operation === 'raster_engrave' && layer.output) {
          if (!(Number(layer.raster_pitch_mm) > 0)) errs.push('raster pitch must be > 0 mm');
          if (!isFinite(Number(layer.raster_angle_deg))) errs.push('raster angle invalid');
          if (!isFinite(Number(layer.raster_angle_step_deg))) errs.push('raster angle step invalid');
        }
        layer._errors = errs;
      });
    }
    function assignSelectionToLayer(layerId) {
      const sel = selectionList();
      const layer = layerById(layerId);
      if (!sel.length) { show('Select one or more vectors first.'); return; }
      if (!layer) { show('Layer not found.'); return; }
      const changed = sel.filter(i => objects[i].layer_id !== layerId);
      activeLayerId = layerId;
      if (!changed.length) {
        show(`${sel.length} selected object(s) already on ${layer.name}.`);
        render();
        return;
      }
      recordHistory();
      changed.forEach(i => { objects[i].layer_id = layerId; });
      show(`Moved ${changed.length} selected object(s) to ${layer.name}.`);
      hideLayerMenu();
      render();
    }
    function showLayerMenu(clientX, clientY) {
      const menu = document.getElementById('layerMenu');
      if (!menu || selectionList().length === 0) return;
      menu.innerHTML = '';
      layers.forEach(layer => {
        const button = document.createElement('button');
        button.innerHTML = `<span class="swatch" style="background:${layer.color}"></span><span>${layer.name}</span>`;
        button.addEventListener('click', () => assignSelectionToLayer(layer.layer_id));
        menu.appendChild(button);
      });
      menu.style.left = `${Math.min(clientX, window.innerWidth - 210)}px`;
      menu.style.top = `${Math.min(clientY, window.innerHeight - 180)}px`;
      menu.classList.add('show');
    }
    function hideLayerMenu() {
      const menu = document.getElementById('layerMenu');
      if (menu) menu.classList.remove('show');
    }

    // Selection picking on canvas
    function pickObjectAt(mxMm, myMm) {
      const tolMm = 1.4 / pixelsPerMm() * 5;
      let bestIndex = -1;
      let bestDist = Infinity;
      objects.forEach((obj, index) => {
        const layer = layerById(obj.layer_id);
        if (layer && layer.visible === false) return;
        for (const poly of objectTransformedPolylines(obj)) {
          for (let i = 1; i < poly.length; i++) {
            const d = pointSegmentDistance([mxMm, myMm], poly[i - 1], poly[i]);
            if (d < bestDist) { bestDist = d; bestIndex = index; }
          }
        }
      });
      return bestDist < tolMm ? bestIndex : -1;
    }
    function objectBounds(index) {
      const obj = objects[index];
      if (!obj) return null;
      const pts = objectTransformedPolylines(obj).flat();
      if (!pts.length) return null;
      const xs = pts.map(p => p[0]);
      const ys = pts.map(p => p[1]);
      return {
        minX: Math.min(...xs),
        maxX: Math.max(...xs),
        minY: Math.min(...ys),
        maxY: Math.max(...ys),
      };
    }
    function rectContainsRect(outer, inner) {
      return inner.minX >= outer.minX && inner.maxX <= outer.maxX && inner.minY >= outer.minY && inner.maxY <= outer.maxY;
    }
    function objectsInRect(rect) {
      const hits = [];
      objects.forEach((obj, index) => {
        const layer = layerById(obj.layer_id);
        if (layer && layer.visible === false) return;
        const box = objectBounds(index);
        if (box && rectContainsRect(rect, box)) hits.push(index);
      });
      return hits;
    }
    function finishMarquee() {
      if (!marquee) return;
      const dx = marquee.endMm[0] - marquee.startMm[0];
      const dy = marquee.endMm[1] - marquee.startMm[1];
      const moved = Math.hypot(dx, dy) > 0.35;
      if (!moved) {
        if (!marquee.additive) clearSelection();
        marquee = null;
        render();
        return;
      }
      const rect = {
        minX: Math.min(marquee.startMm[0], marquee.endMm[0]),
        maxX: Math.max(marquee.startMm[0], marquee.endMm[0]),
        minY: Math.min(marquee.startMm[1], marquee.endMm[1]),
        maxY: Math.max(marquee.startMm[1], marquee.endMm[1]),
      };
      const hits = objectsInRect(rect);
      if (!marquee.additive) selected = new Set(hits);
      else hits.forEach(i => selected.add(i));
      lastSelected = hits.length ? hits[hits.length - 1] : lastSelected;
      marquee = null;
      render();
    }
    function endpointKey(pt, toleranceMm) {
      return `${Math.round(pt[0] / toleranceMm)},${Math.round(pt[1] / toleranceMm)}`;
    }
    function connectedEndpointKeys(index, toleranceMm) {
      const keys = new Set();
      for (const poly of objectTransformedPolylines(objects[index])) {
        if (!poly.length) continue;
        keys.add(endpointKey(poly[0], toleranceMm));
        keys.add(endpointKey(poly[poly.length - 1], toleranceMm));
      }
      return keys;
    }
    function selectConnectedFrom(startIndex) {
      if (startIndex < 0 || startIndex >= objects.length) return;
      const toleranceMm = Math.max(0.001, Number(document.getElementById('joinTolerance').value) || 0.05);
      const objectKeys = objects.map((obj, index) => {
        const layer = layerById(obj.layer_id);
        if (layer && layer.visible === false) return new Set();
        return connectedEndpointKeys(index, toleranceMm);
      });
      const keyToObjects = new Map();
      objectKeys.forEach((keys, index) => {
        keys.forEach(key => {
          if (!keyToObjects.has(key)) keyToObjects.set(key, new Set());
          keyToObjects.get(key).add(index);
        });
      });
      const queue = [startIndex];
      const component = new Set([startIndex]);
      for (let qi = 0; qi < queue.length; qi++) {
        const index = queue[qi];
        objectKeys[index].forEach(key => {
          (keyToObjects.get(key) || []).forEach(next => {
            if (component.has(next)) return;
            component.add(next);
            queue.push(next);
          });
        });
      }
      selected = component;
      lastSelected = startIndex;
      render();
    }
    function pointSegmentDistance(p, a, b) {
      const ax = a[0], ay = a[1], bx = b[0], by = b[1];
      const dx = bx - ax, dy = by - ay;
      const len2 = dx * dx + dy * dy;
      if (len2 === 0) return Math.hypot(p[0] - ax, p[1] - ay);
      const t = Math.max(0, Math.min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / len2));
      return Math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy);
    }

    function canvasToCanvasPx(event) {
      const rect = canvas.getBoundingClientRect();
      return [
        (event.clientX - rect.left) * canvas.width / rect.width,
        (event.clientY - rect.top) * canvas.height / rect.height,
      ];
    }
    function canvasToMm(event) {
      const [px, py] = canvasToCanvasPx(event);
      return pxToMm(px, py);
    }

    canvas.addEventListener('pointerdown', event => {
      const [mxMm, myMm] = canvasToMm(event);
      if (event.button === 1 || spaceHeld || toolMode === 'pan') {
        panning = { startPx: canvasToCanvasPx(event), startView: [viewX, viewY] };
        canvas.classList.add('panning');
        canvas.setPointerCapture(event.pointerId);
        return;
      }
      const sel = selectionList();
      const hit = pickObjectAt(mxMm, myMm);
      if (hit >= 0) {
        if (event.shiftKey) toggleSelected(hit);
        else if (!isSelected(hit)) selectOne(hit);
        const drags = selectionList().map(i => ({ i, x: objects[i].x, y: objects[i].y }));
        dragging = { startMm: [mxMm, myMm], drags, recorded: false };
        canvas.setPointerCapture(event.pointerId);
        render();
        return;
      }
      marquee = {
        startMm: [mxMm, myMm],
        endMm: [mxMm, myMm],
        additive: event.shiftKey,
      };
      canvas.setPointerCapture(event.pointerId);
      render();
    });
    canvas.addEventListener('pointermove', event => {
      const [mxMm, myMm] = canvasToMm(event);
      document.getElementById('cursorChip').textContent = `x= ${mxMm.toFixed(1)} y= ${myMm.toFixed(1)} mm`;
      if (panning) {
        const [px, py] = canvasToCanvasPx(event);
        const s = pixelsPerMm();
        viewX = panning.startView[0] - (px - panning.startPx[0]) / s;
        viewY = panning.startView[1] + (py - panning.startPx[1]) / s;
        render();
        return;
      }
      if (dragging) {
        const dx = mxMm - dragging.startMm[0];
        const dy = myMm - dragging.startMm[1];
        if (!dragging.recorded && Math.hypot(dx, dy) > 0.02) {
          recordHistory();
          dragging.recorded = true;
        }
        for (const d of dragging.drags) {
          objects[d.i].x = d.x + dx;
          objects[d.i].y = d.y + dy;
        }
        render();
        return;
      }
      if (marquee) {
        marquee.endMm = [mxMm, myMm];
        render();
      }
    });
    canvas.addEventListener('pointerup', () => {
      if (marquee) finishMarquee();
      dragging = null;
      panning = null;
      canvas.classList.remove('panning');
    });
    canvas.addEventListener('dblclick', event => {
      event.preventDefault();
      const [mxMm, myMm] = canvasToMm(event);
      const hit = pickObjectAt(mxMm, myMm);
      if (hit >= 0) selectConnectedFrom(hit);
    });
    canvas.addEventListener('contextmenu', event => {
      event.preventDefault();
      const [mxMm, myMm] = canvasToMm(event);
      const hit = pickObjectAt(mxMm, myMm);
      if (hit >= 0 && !isSelected(hit)) selectOne(hit);
      if (selectionList().length) {
        render();
        showLayerMenu(event.clientX, event.clientY);
      } else {
        hideLayerMenu();
      }
    });
    canvas.addEventListener('wheel', event => {
      event.preventDefault();
      const [mxMm, myMm] = canvasToMm(event);
      const factor = Math.exp(-event.deltaY * 0.0015);
      zoom = Math.min(40, Math.max(0.2, zoom * factor));
      // Keep cursor mm position stable across zoom.
      const [mx2, my2] = canvasToMm(event);
      viewX += mxMm - mx2;
      viewY += myMm - my2;
      render();
    }, { passive: false });
    document.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        redo();
        return;
      }
      if (event.target && ['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName)) return;
      if (event.code === 'Space') { spaceHeld = true; canvas.classList.add('panMode'); }
      if (event.key === 'Delete' || event.key === 'Backspace') deleteSelection();
      if (event.key === 'f' || event.key === 'F') fitToObjects();
      if (event.key === '0') resetView();
    });
    document.addEventListener('keyup', event => {
      if (event.code === 'Space') {
        spaceHeld = false;
        canvas.classList.toggle('panMode', toolMode === 'pan');
      }
    });
    document.addEventListener('pointerdown', event => {
      if (!event.target.closest || !event.target.closest('#layerMenu')) hideLayerMenu();
    });

    function fitToObjects() {
      const polys = transformedPolylines();
      if (!polys.length) { resetView(); return; }
      const xs = polys.flat().map(p => p[0]);
      const ys = polys.flat().map(p => p[1]);
      const w = Math.max(20, Math.max(...xs) - Math.min(...xs));
      const h = Math.max(20, Math.max(...ys) - Math.min(...ys));
      const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
      const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
      zoom = Math.min(40, Math.min(FIELD_W_MM / w, FIELD_H_MM / h) * 0.85);
      viewX = cx; viewY = cy;
      render();
    }
    function resetView() { zoom = 1; viewX = 0; viewY = 0; render(); }
    function deleteSelection() {
      const sel = selectionList();
      if (!sel.length) return;
      recordHistory();
      for (let i = sel.length - 1; i >= 0; i--) objects.splice(sel[i], 1);
      clearSelection();
      render();
    }

    function bboxForIndices(indices) {
      const pts = indices.flatMap(i => objectTransformedPolylines(objects[i]).flat());
      if (!pts.length) return null;
      const xs = pts.map(p => p[0]);
      const ys = pts.map(p => p[1]);
      return {
        minX: Math.min(...xs),
        maxX: Math.max(...xs),
        minY: Math.min(...ys),
        maxY: Math.max(...ys),
        get cx() { return (this.minX + this.maxX) / 2; },
        get cy() { return (this.minY + this.maxY) / 2; },
        get width() { return this.maxX - this.minX; },
        get height() { return this.maxY - this.minY; },
      };
    }
    function moveIndicesCenterTo(indices, targetX, targetY) {
      const box = bboxForIndices(indices);
      if (!box) return;
      const dx = targetX - box.cx;
      const dy = targetY - box.cy;
      indices.forEach(i => {
        objects[i].x += dx;
        objects[i].y += dy;
      });
      render();
    }
    function centerSelection() {
      const sel = selectionList();
      if (!sel.length) return;
      recordHistory();
      moveIndicesCenterTo(sel, 0, 0);
    }
    function centerAll() {
      if (!objects.length) return;
      recordHistory();
      moveIndicesCenterTo(objects.map((_, i) => i), 0, 0);
    }
    function moveSelectionToReference() {
      const sel = selectionList();
      if (!sel.length) return;
      recordHistory();
      moveIndicesCenterTo(
        sel,
        parseNumericInput('refX', 0),
        parseNumericInput('refY', 0),
      );
    }
    function drawingBox() {
      return objects.length ? bboxForIndices(objects.map((_, i) => i)) : null;
    }
    function setReferencePoint(kind) {
      const box = drawingBox();
      if (!box) return;
      const points = {
        center: [box.cx, box.cy],
        topLeft: [box.minX, box.maxY],
        topRight: [box.maxX, box.maxY],
        bottomLeft: [box.minX, box.minY],
        bottomRight: [box.maxX, box.minY],
      };
      const point = points[kind];
      if (!point) return;
      document.getElementById('refX').value = point[0].toFixed(2);
      document.getElementById('refY').value = point[1].toFixed(2);
    }
    function groupSelection() {
      const sel = selectionList();
      if (sel.length < 2) { show('Select at least two objects to group.'); return; }
      recordHistory();
      const layerId = objects[sel[0]].layer_id;
      const groupedPolylines = sel.flatMap(i => objectTransformedPolylines(objects[i]));
      const groupedName = `${sel.length} grouped objects`;
      for (let i = sel.length - 1; i >= 0; i--) objects.splice(sel[i], 1);
      objects.push({
        name: groupedName,
        polylines: groupedPolylines,
        pointCount: groupedPolylines.reduce((acc, p) => acc + p.length, 0),
        x: 0, y: 0, scale: 1, rotation: 0,
        layer_id: layerId,
        grouped: true,
      });
      selectOne(objects.length - 1);
      render();
    }
    function ungroupSelection() {
      const sel = selectionList();
      if (!sel.length) { show('Select a grouped object to ungroup.'); return; }
      recordHistory();
      const created = [];
      const selectedObjects = sel.map(i => ({ index: i, obj: objects[i] }));
      for (let i = sel.length - 1; i >= 0; i--) objects.splice(sel[i], 1);
      selectedObjects.forEach(({ obj }) => {
        const polylines = objectTransformedPolylines(obj);
        if (polylines.length <= 1 && !obj.grouped) {
          objects.push(obj);
          created.push(objects.length - 1);
          return;
        }
        polylines.forEach((polyline, index) => {
          objects.push({
            name: `${obj.name} · part ${index + 1}`,
            polylines: [polyline],
            pointCount: polyline.length,
            x: 0, y: 0, scale: 1, rotation: 0,
            layer_id: obj.layer_id,
          });
          created.push(objects.length - 1);
        });
      });
      selected = new Set(created);
      lastSelected = created.length ? created[created.length - 1] : -1;
      render();
    }

    ['x','y','scale','rotation'].forEach(id => document.getElementById(id).addEventListener('input', updateSelected));
    ['power','frequency','pulseWidth','markSpeed','confirm'].forEach(id => document.getElementById(id).addEventListener('input', updateSummary));

    document.getElementById('file').addEventListener('change', async event => {
      const file = event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const imported = await getJson('/api/import', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({name: file.name, data: reader.result})
          });
          recordHistory();
          const startIndex = objects.length;
          imported.polylines.forEach((polyline, index) => {
            objects.push({
              name: imported.polylines.length === 1 ? imported.name : `${imported.name} · path ${index + 1}`,
              polylines: [polyline],
              pointCount: polyline.length,
              x: 0, y: 0, scale: 1, rotation: 0,
              layer_id: activeLayerId || (layers[0] && layers[0].layer_id) || 'vector-engrave',
            });
          });
          selected = new Set(objects.slice(startIndex).map((_, offset) => startIndex + offset));
          lastSelected = objects.length - 1;
          show(imported);
          render();
        } catch (error) { show(error); }
      };
      reader.readAsDataURL(file);
    });
    document.getElementById('delete').addEventListener('click', deleteSelection);
    document.getElementById('fit').addEventListener('click', () => {
      const sel = selectionList();
      if (!sel.length) return;
      recordHistory();
      sel.forEach(i => { objects[i].x = 0; objects[i].y = 0; objects[i].scale = 1; objects[i].rotation = 0; });
      render();
    });
    document.getElementById('zoomFit').addEventListener('click', fitToObjects);
    document.getElementById('zoomReset').addEventListener('click', resetView);
    document.getElementById('centerSelection').addEventListener('click', centerSelection);
    document.getElementById('centerAll').addEventListener('click', centerAll);
    document.getElementById('moveToRef').addEventListener('click', moveSelectionToReference);
    document.getElementById('refCenter').addEventListener('click', () => setReferencePoint('center'));
    document.getElementById('refTopLeft').addEventListener('click', () => setReferencePoint('topLeft'));
    document.getElementById('refTopRight').addEventListener('click', () => setReferencePoint('topRight'));
    document.getElementById('refBottomLeft').addEventListener('click', () => setReferencePoint('bottomLeft'));
    document.getElementById('refBottomRight').addEventListener('click', () => setReferencePoint('bottomRight'));
    document.getElementById('groupSelection').addEventListener('click', groupSelection);
    document.getElementById('ungroupSelection').addEventListener('click', ungroupSelection);
    document.getElementById('selectAllObjects').addEventListener('click', () => {
      selected = new Set(objects.map((_, i) => i));
      lastSelected = objects.length - 1;
      resetTransformEdit();
      render();
    });
    document.getElementById('clearSelectionBtn').addEventListener('click', () => {
      clearSelection();
      render();
    });
    document.getElementById('undoBtn').addEventListener('click', undo);
    document.getElementById('redoBtn').addEventListener('click', redo);
    document.getElementById('selectTool').addEventListener('click', () => setToolMode('select'));
    document.getElementById('panTool').addEventListener('click', () => setToolMode('pan'));
    document.getElementById('toggleLeft').addEventListener('click', () => toggleSidebar('left'));
    document.getElementById('toggleRight').addEventListener('click', () => toggleSidebar('right'));
    ['x','y','scale','rotation'].forEach(id => {
      document.getElementById(id).addEventListener('blur', resetTransformEdit);
    });

    document.getElementById('joinLines').addEventListener('click', async () => {
      const sel = selectionList();
      if (!sel.length) { show('Select the line fragments to join first. Double-click a connected shape or window-select it.'); return; }
      const tolerance_mm = Number(document.getElementById('joinTolerance').value);
      const polylines = sel.flatMap(i => objectTransformedPolylines(objects[i]));
      try {
        const result = await getJson('/api/join', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({polylines, tolerance_mm}),
        });
        if (result.joins_performed === 0 && result.output_paths === sel.length) {
          show({
            ...result,
            note: 'No endpoints were within the join tolerance. Increase tolerance or select line fragments that share endpoints.',
          });
          return;
        }
        recordHistory();
        const layerId = objects[sel[0]].layer_id;
        for (let i = sel.length - 1; i >= 0; i--) objects.splice(sel[i], 1);
        const startIndex = objects.length;
        result.polylines.forEach((polyline, index) => {
          objects.push({
            name: result.polylines.length === 1 ? 'Joined path' : `Joined path ${index + 1}`,
            polylines: [polyline],
            pointCount: polyline.length,
            x: 0, y: 0, scale: 1, rotation: 0,
            layer_id: layerId,
          });
        });
        selected = new Set(objects.slice(startIndex).map((_, offset) => startIndex + offset));
        lastSelected = objects.length - 1;
        show(result);
        render();
      } catch (error) { show(error); }
    });

    function mirrorPolylines(polys, mx, my) {
      if (!mx && !my) return polys;
      const sx = mx ? -1 : 1;
      const sy = my ? -1 : 1;
      return polys.map(poly => poly.map(pt => [pt[0] * sx, pt[1] * sy]));
    }
    function buildJobPayload() {
      const selectedOnly = document.getElementById('selectedOnly').checked;
      const selectedIndexes = new Set(selectionList());
      const jobObjects = selectedOnly ? objects.filter((_, index) => selectedIndexes.has(index)) : objects;
      const mx = document.getElementById('mirrorX').checked;
      const my = document.getElementById('mirrorY').checked;
      return {
        machine_id: activeMachine ? activeMachine.machine_id : 'jpt-m7-200',
        objects: jobObjects.map(obj => ({
          name: obj.name,
          layer_id: obj.layer_id,
          polylines: mirrorPolylines(objectTransformedPolylines(obj), mx, my),
        })),
        layers: layers.map(layer => ({...layer})),
        live: liveParams(),
      };
    }
    function liveParams() {
      return {
        text: '',
        power: Number(document.getElementById('power').value),
        frequency_khz: Number(document.getElementById('frequency').value),
        pulse_width_ns: Number(document.getElementById('pulseWidth').value),
        mark_speed: Number(document.getElementById('markSpeed').value),
        repeat_count: Math.max(1, Math.floor(Number(document.getElementById('markRepeat').value) || 1)),
        size_mm: 10,
        confirm: document.getElementById('confirm').value,
        arm: true,
      };
    }

    async function postJson(path, body) {
      try {
        return await getJson(path, {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify(body),
        });
      } catch (error) { show(error); throw error; }
    }

    document.getElementById('plan').addEventListener('click', async () => {
      try { show(await postJson('/api/plan', buildJobPayload())); } catch {}
    });
    document.getElementById('exportGcode').addEventListener('click', async () => {
      try {
        const result = await postJson('/api/export/gcode', buildJobPayload());
        const blob = new Blob([result.content], { type: 'text/plain;charset=utf-8' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = result.filename || 'openlaser-job.gcode';
        link.click();
        URL.revokeObjectURL(link.href);
        show({ ...result, content: `${result.content.length} bytes downloaded` });
      } catch {}
    });
    document.getElementById('frame').addEventListener('click', async () => {
      try { show(await postJson('/api/frame', buildJobPayload())); } catch {}
    });
    document.getElementById('frameStart').addEventListener('click', async () => {
      try { show(await postJson('/api/frame/start', buildJobPayload())); } catch {}
    });
    document.getElementById('frameStop').addEventListener('click', async () => {
      try { show(await getJson('/api/frame/stop', { method: 'POST', headers: {'content-type':'application/json'}, body: '{}' })); } catch (e) { show(e); }
    });
    document.getElementById('stopEngraving').addEventListener('click', async () => {
      try {
        show(await getJson('/api/stop', { method: 'POST', headers: {'content-type':'application/json'}, body: '{}' }));
      } catch (e) { show(e); }
    });

    function openModal({ title, body, onConfirm }) {
      document.getElementById('modalTitle').textContent = title;
      document.getElementById('modalBody').innerHTML = body;
      document.getElementById('modalScrim').classList.add('show');
      const confirm = document.getElementById('modalConfirm');
      const cancel = document.getElementById('modalCancel');
      const close = () => { document.getElementById('modalScrim').classList.remove('show'); confirm.onclick = null; cancel.onclick = null; };
      confirm.onclick = () => { close(); onConfirm(); };
      cancel.onclick = close;
    }

    document.getElementById('mark').addEventListener('click', async () => {
      const payload = buildJobPayload();
      try {
        const preview = await postJson('/api/plan', payload);
        const params = payload.live;
        const selectedOnly = document.getElementById('selectedOnly').checked;
        const errs = (preview.safety && preview.safety.errors) || [];
        const warns = (preview.safety && preview.safety.warnings) || [];
        if (errs.length) { show({ refused: errs }); return; }
        const body = `
          <p>Laser will emit now. Power ${params.power}%,
          Frequency ${params.frequency_khz} kHz,
          Pulse ${params.pulse_width_ns} ns,
          Speed ${params.mark_speed} mm/s.</p>
          <p>${preview.emitting_paths || 0} path(s) across ${preview.emitting_layers || 0} layer(s).
          Bounding box ${preview.bbox ? preview.bbox.width.toFixed(2) + ' × ' + preview.bbox.height.toFixed(2) + ' mm' : '—'}.</p>
          <p>Scope: ${selectedOnly ? 'selected objects only' : 'all objects'}. Repeat: ${params.repeat_count}×.</p>
          ${warns.length ? `<div class="warns">${warns.join('<br>')}</div>` : ''}
        `;
        openModal({
          title: 'Confirm marking',
          body,
          onConfirm: async () => {
            try { show(await postJson('/api/mark', payload)); } catch {}
          }
        });
      } catch {}
    });

    document.getElementById('dotTest').addEventListener('click', () => firstBurnTest('dot'));
    document.getElementById('lineTest').addEventListener('click', () => firstBurnTest('line'));

    async function firstBurnTest(kind) {
      try {
        const preview = await postJson('/api/test/preview', { kind });
        const errs = (preview.safety && preview.safety.errors) || [];
        if (errs.length) { show({ refused: errs }); return; }
        openModal({
          title: kind === 'dot' ? 'Confirm test dot' : 'Confirm 5 mm line',
          body: `<p>Laser will emit now. Power ${preview.params.power}%, Frequency ${preview.params.frequency_khz} kHz,
                  Pulse ${preview.params.pulse_width_ns} ns, Speed ${preview.params.mark_speed} mm/s.</p>
                  <p>This is a first-burn helper for fixturing tests.</p>`,
          onConfirm: async () => {
            const armPayload = { ...preview.params, arm: true, confirm: document.getElementById('confirm').value, kind };
            try { show(await postJson('/api/test/run', armPayload)); } catch {}
          }
        });
      } catch {}
    }

    function setLaserChip(connected, mockActive) {
      const status = document.getElementById('status');
      if (activeMachine && !activeMachine.direct_control) {
        status.classList.add('ok');
        status.querySelector('span:last-child').textContent = 'G-code output ready';
        return;
      }
      status.classList.toggle('ok', Boolean(connected) || Boolean(mockActive));
      status.querySelector('span:last-child').textContent = mockActive
        ? 'Mock mode — no hardware'
        : (connected ? 'JCZ/LMC USB connected' : 'Laser not detected — check power/USB');
    }
    async function refreshDetect() {
      const data = await getJson('/api/detect');
      setLaserChip(data.connected, false);
    }

    // --- Server heartbeat: live connection banner + laser presence ---
    const HEALTH_INTERVAL_MS = 3000;
    let serverOnline = true;
    let lastServerContact = Date.now();
    function setServerState(online) {
      const banner = document.getElementById('connBanner');
      if (online) {
        lastServerContact = Date.now();
        if (!serverOnline) {
          serverOnline = true;
          document.body.classList.remove('serverOffline');
          banner.className = 'back';
          banner.innerHTML = 'Server reconnected — controls re-enabled';
          setTimeout(() => { if (serverOnline) banner.className = ''; }, 2500);
          loadSafety(); loadConfig();  // re-sync state across a server restart
        }
        return;
      }
      if (serverOnline) {
        serverOnline = false;
        document.body.classList.add('serverOffline');
        banner.className = 'offline';
        banner.innerHTML = '<span class="pulse">&#9888;</span> Server connection lost — hardware controls disabled, retrying&hellip;' +
          '<span class="sub">The OpenLaser server is not responding (its Terminal window may have been closed). ' +
          'Restart it, e.g. <code>./.venv/bin/python -m mopa_luiz ui</code> — last contact <span id="connLastSeen">0s</span> ago.</span>';
      }
      const seen = document.getElementById('connLastSeen');
      if (seen) seen.textContent = Math.max(0, Math.round((Date.now() - lastServerContact) / 1000)) + 's';
    }
    async function heartbeat() {
      try {
        const res = await fetch('/api/health', { cache: 'no-store', signal: AbortSignal.timeout(2500) });
        if (!res.ok) throw new Error('http ' + res.status);
        const data = await res.json();
        setServerState(true);
        setLaserChip(data.laser_connected, data.mock);
      } catch {
        setServerState(false);
      }
    }
    setInterval(heartbeat, HEALTH_INTERVAL_MS);
    function applyFieldCalibration(nominalMm, correction) {
      if (Number.isFinite(nominalMm) && nominalMm > 0) fieldNominalMm = nominalMm;
      const factor = Number.isFinite(correction) && correction > 0 ? correction : 1;
      FIELD_W_MM = fieldNominalMm * factor;
      FIELD_H_MM = fieldNominalMm * factor;
      const chip = document.getElementById('fieldChip');
      if (chip) chip.textContent = `Field ${FIELD_W_MM.toFixed(1)} × ${FIELD_H_MM.toFixed(1)} mm`;
    }
    function applyMachine(machine) {
      activeMachine = machine;
      if (!machine) return;
      if (machine.kind === 'fiber_galvo') {
        const correction = Number(document.getElementById('scaleCorrection')?.value || 1);
        applyFieldCalibration(fieldNominalMm, correction);
      } else {
        FIELD_W_MM = Number(machine.width_mm);
        FIELD_H_MM = Number(machine.height_mm);
        document.getElementById('fieldChip').textContent = `Bed ${FIELD_W_MM.toFixed(0)} × ${FIELD_H_MM.toFixed(0)} mm`;
      }
      document.getElementById('exportGcode').disabled = machine.output_format !== 'gcode';
      document.getElementById('mark').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('frame').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('frameStart').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('frameStop').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('stopEngraving').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('armControl').style.display = machine.direct_control ? '' : 'none';
      document.getElementById('fiberAdvanced').style.display = machine.kind === 'fiber_galvo' ? '' : 'none';
      document.getElementById('powerMode').textContent = machine.direct_control
        ? 'ARM required for emission' : 'Export only · no direct machine motion';
      document.getElementById('status').querySelector('span:last-child').textContent = machine.direct_control
        ? 'Checking JCZ/LMC USB' : 'File output · connect with a GRBL sender';
      resetView();
    }
    async function loadMachines() {
      const data = await getJson('/api/machines');
      machines = data.machines || [];
      const select = document.getElementById('machineSelect');
      select.innerHTML = machines.map(machine => `<option value="${machine.machine_id}">${machine.name}</option>`).join('');
      let saved = null;
      try { saved = localStorage.getItem('openlaser.machine'); } catch {}
      const machine = machines.find(item => item.machine_id === saved) || machines[0];
      select.value = machine.machine_id;
      applyMachine(machine);
      select.addEventListener('change', () => {
        const next = machines.find(item => item.machine_id === select.value);
        try { localStorage.setItem('openlaser.machine', select.value); } catch {}
        applyMachine(next);
        render();
      });
    }
    async function loadConfig() {
      const data = await getJson('/api/config');
      const config = document.getElementById('config');
      config.innerHTML = '';
      ['FIELDSIZE','MINPWMFREQ','MAXPWMFREQ','m_nIPGSerialNo','m_bEnableIPGSetPulseWidth'].forEach(key => {
        const row = document.createElement('div');
        row.className = 'kv';
        row.innerHTML = `<span>${key}</span><strong>${data[key] ?? ''}</strong>`;
        config.appendChild(row);
      });
      // Field size x scale correction is the single source of truth for the
      // work area: the canvas must match what the mark path uses so the screen
      // and the hardware can never drift apart.
      if (!activeMachine || activeMachine.kind === 'fiber_galvo') {
        applyFieldCalibration(Number(data.FIELDSIZE), Number(data.SCALE_CORRECTION));
      }
      const scInput = document.getElementById('scaleCorrection');
      if (scInput && Number.isFinite(Number(data.SCALE_CORRECTION))) {
        scInput.value = String(data.SCALE_CORRECTION);
      }
    }
    function scheduleWorkspaceSave() {
      if (!workspaceReady) return;
      clearTimeout(workspaceSaveTimer);
      workspaceSaveTimer = setTimeout(async () => {
        const body = JSON.stringify({ objects });
        if (body === lastWorkspaceJson) return;
        try {
          await getJson('/api/workspace/save', {
            method: 'POST', headers: {'content-type': 'application/json'}, body,
          });
          lastWorkspaceJson = body;
        } catch (error) { console.warn('workspace autosave failed', error); }
      }, 1200);
    }
    async function loadWorkspace() {
      try {
        const data = await getJson('/api/workspace');
        if (Array.isArray(data.objects) && data.objects.length) {
          objects.splice(0, objects.length, ...data.objects);
          clearSelection();
        }
        lastWorkspaceJson = JSON.stringify({ objects });
      } catch (error) { console.warn('workspace restore failed', error); }
      workspaceReady = true;
    }
    async function loadSafety() {
      safety = await getJson('/api/safety');
      const pill = document.getElementById('powerMode');
      if (pill) {
        pill.classList.remove('bad', 'warning');
        pill.textContent = 'ARM required for emission';
      }
      updateSummary();
    }
    async function loadLayers() {
      const data = await getJson('/api/layers');
      layers = data.layers;
      activeLayerId = layers[0].layer_id;
      render();
    }
    document.getElementById('inspectProfile').addEventListener('click', async () => {
      try { show(await getJson('/api/profile/inspect')); } catch (error) { show(error); }
    });

    (function initMirror() {
      const mx = document.getElementById('mirrorX');
      const my = document.getElementById('mirrorY');
      try {
        mx.checked = localStorage.getItem('openmopa.mirrorX') === '1';
        my.checked = localStorage.getItem('openmopa.mirrorY') === '1';
      } catch {}
      mx.addEventListener('change', () => { try { localStorage.setItem('openmopa.mirrorX', mx.checked ? '1' : '0'); } catch {} });
      my.addEventListener('change', () => { try { localStorage.setItem('openmopa.mirrorY', my.checked ? '1' : '0'); } catch {} });
    })();

    (function initScaleCorrection() {
      const input = document.getElementById('scaleCorrection');
      if (!input) return;
      input.addEventListener('change', async () => {
        let value = Number(String(input.value).replace(',', '.'));
        if (!Number.isFinite(value) || value < 0.5 || value > 2) value = 1;
        try {
          const res = await postJson('/api/calibration/save', { scale_correction: value });
          input.value = String(res.scale_correction);
          applyFieldCalibration(fieldNominalMm, Number(res.scale_correction));
          render();
          show(`Scale correction saved: ${res.scale_correction} (effective field ${(fieldNominalMm * res.scale_correction).toFixed(2)} mm)`);
        } catch (error) { show(error); }
      });
    })();

    loadSafety().then(loadMachines).then(loadLayers).then(loadConfig).then(loadWorkspace).then(refreshDetect).then(render).then(heartbeat);
  </script>
</body>
</html>
"""


class UiHandler(BaseHTTPRequestHandler):
    markcfg: Path

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: HTTPStatus, payload: dict) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            data = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/health":
            # Heartbeat for the UI: must stay fast and never raise. pyusb
            # enumeration only — no device open, safe during running jobs.
            try:
                laser = bool(pyusb_probe().get("devices"))
            except Exception:
                laser = False
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "laser_connected": laser,
                "frame_loop_running": FRAME_LOOP.running,
                "mock": mock_mode(),
            })
            return
        if path == "/api/detect":
            self.send_json(HTTPStatus.OK, detect_report())
            return
        if path == "/api/machines":
            self.send_json(HTTPStatus.OK, {"machines": machine_catalog()})
            return
        if path == "/api/config":
            summary = config_summary(load_markcfg(self.markcfg))
            summary["SCALE_CORRECTION"] = load_scale_correction()
            self.send_json(HTTPStatus.OK, summary)
            return
        if path == "/api/board":
            self.send_json(HTTPStatus.OK, read_board_status())
            return
        if path == "/api/frame/status":
            self.send_json(HTTPStatus.OK, FRAME_LOOP.status())
            return
        if path == "/api/safety":
            self.send_json(HTTPStatus.OK, {
                "max_power_percent": 100.0,
                "arm_required": True,
            })
            return
        if path == "/api/layers/defaults":
            self.send_json(HTTPStatus.OK, {"layers": [layer.to_dict() for layer in default_layers()]})
            return
        if path == "/api/layers":
            self.send_json(HTTPStatus.OK, {"layers": load_saved_layers(), "path": str(LAYER_SETTINGS_PATH)})
            return
        if path == "/api/workspace":
            self.send_json(HTTPStatus.OK, load_workspace())
            return
        if path == "/api/profile/inspect":
            self.send_json(HTTPStatus.OK, inspect_profile_data(load_markcfg(self.markcfg)))
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def read_payload(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_payload()
            if path == "/api/stop":
                frame_result = FRAME_LOOP.stop()
                hardware_result = stop_active_hardware_jobs()
                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "operation": "emergency_stop",
                    "frame": frame_result,
                    "hardware": hardware_result,
                })
                return
            if path == "/api/import":
                self.send_json(HTTPStatus.OK, import_geometry(
                    str(payload.get("name", "import")),
                    str(payload.get("data", "")),
                ))
                return
            if path == "/api/join":
                polylines = payload.get("polylines") or []
                tol = float(payload.get("tolerance_mm", 0.05))
                result = join_lines(polylines, tolerance_mm=tol)
                self.send_json(HTTPStatus.OK, {
                    **result.to_dict(),
                    "polylines": [list(map(list, poly)) for poly in result.polylines],
                })
                return
            if path == "/api/raster/preview":
                polylines = payload.get("polylines") or []
                result = hatch_polylines(
                    polylines,
                    angle_deg=float(payload.get("angle_deg", 0.0)),
                    pitch_mm=float(payload.get("pitch_mm", 0.1)),
                    passes=max(1, int(payload.get("passes", 1))),
                    angle_step_deg=float(payload.get("angle_step_deg", 90.0)),
                )
                self.send_json(HTTPStatus.OK, result.to_dict())
                return
            if path == "/api/layers/save":
                layers = save_layers(payload.get("layers"))
                self.send_json(HTTPStatus.OK, {"ok": True, "layers": layers, "path": str(LAYER_SETTINGS_PATH)})
                return
            if path == "/api/workspace/save":
                self.send_json(HTTPStatus.OK, save_workspace(payload.get("objects")))
                return
            if path == "/api/calibration/save":
                value = save_scale_correction(payload.get("scale_correction"))
                self.send_json(HTTPStatus.OK, {"ok": True, "scale_correction": value})
                return

            cfg = load_markcfg(self.markcfg)
            objects = payload.get("objects")
            layer_payloads = payload.get("layers")
            live = payload.get("live") or {}

            if path == "/api/test/preview":
                kind = str(payload.get("kind", "dot"))
                params = first_burn_params(kind)
                safety = evaluate_emission(
                    cfg=cfg,
                    power_percent=params["power"],
                    frequency_khz=params["frequency_khz"],
                    pulse_width_ns=params["pulse_width_ns"],
                    intends_emission=True,
                    arm=False,  # preview only
                    confirm="",
                    operation="vector_engrave",
                    paths_count=1,
                )
                self.send_json(HTTPStatus.OK, {
                    "params": params,
                    "kind": kind,
                    "safety": {
                        # Drop the ARM-missing error from preview — UI requires ARM at run time.
                        "errors": [e for e in safety.errors if "arm=true" not in e],
                        "warnings": safety.warnings,
                        "arm_required": True,
                    },
                })
                return

            if path == "/api/test/run":
                FRAME_LOOP.stop()
                clear_stop_request()
                kind = str(payload.get("kind", "dot"))
                params = first_burn_params(kind)
                arm = bool(payload.get("arm"))
                confirm = str(payload.get("confirm", ""))
                polylines = first_burn_polylines(kind)
                self.send_json(
                    HTTPStatus.OK,
                    run_hardware_job(
                        "mark_polylines",
                        cfg,
                        JobParams(
                            text="",
                            power=params["power"],
                            frequency_khz=params["frequency_khz"],
                            pulse_width_ns=params["pulse_width_ns"],
                            size_mm=10.0,
                            mark_speed=params["mark_speed"],
                        ),
                        polylines,
                        arm=arm,
                        confirm=confirm,
                    ),
                )
                return

            params = job_params_from_payload(live)
            layers = [layer_from_dict(item) for item in layer_payloads] if layer_payloads else default_layers()
            obj_list = list(objects) if objects else []
            jobs = group_by_layer(obj_list, layers)
            framing = framing_jobs(jobs)
            emitting = emitting_jobs(jobs)
            framing_polylines = [poly for job in framing for poly in job.polylines]
            emitting_polylines = [poly for job in emitting for poly in job.polylines]
            machine = machine_by_id(str(payload.get("machine_id") or "jpt-m7-200"))

            if path == "/api/export/gcode":
                program = compile_gcode(machine, jobs)
                self.send_json(HTTPStatus.OK, {
                    **program.to_dict(),
                    "ok": True,
                    "filename": f"openlaser-{machine.kind}.gcode",
                    "machine": machine.to_dict(),
                })
                return

            if path == "/api/plan":
                bbox = bounding_box(framing_polylines or emitting_polylines)
                if machine.output_format == "gcode":
                    program = None
                    plan_errors: list[str] = []
                    try:
                        program = compile_gcode(machine, jobs)
                    except ValueError as exc:
                        plan_errors.append(str(exc))
                    self.send_json(HTTPStatus.OK, {
                        "mode": "gcode-preview",
                        "emission_enabled": False,
                        "machine": machine.to_dict(),
                        "object_count": len(obj_list),
                        "layer_count": len(layers),
                        "emitting_layers": sum(1 for job in emitting if job.polylines),
                        "framing_paths": len(framing_polylines),
                        "emitting_paths": len(emitting_polylines),
                        "bbox": bbox,
                        "layers": [
                            {
                                "layer": job.layer.to_dict(),
                                "path_count": len(job.polylines),
                                "would_emit": job.layer.emits_when_marking() and bool(job.polylines),
                            }
                            for job in jobs
                        ],
                        "layer_errors": {},
                        "safety": {
                            "ok": not plan_errors,
                            "emission_will_occur": False,
                            "arm_ok": True,
                            "errors": plan_errors,
                            "warnings": ["Exported G-code must be framed and verified in your GRBL sender."],
                        },
                        "gcode": None if program is None else {
                            "path_count": program.path_count,
                            "point_count": program.point_count,
                            "estimated_distance_mm": program.estimated_distance_mm,
                        },
                    })
                    return
                layer_errors = validate_layers(layers, cfg)
                primary = next((job for job in emitting if job.polylines), None)
                if primary is None:
                    primary_safety = evaluate_emission(
                        cfg=cfg,
                        power_percent=params.power,
                        frequency_khz=params.frequency_khz,
                        pulse_width_ns=params.pulse_width_ns,
                        intends_emission=False,
                        arm=False,
                        confirm="",
                        operation="frame_only",
                        paths_count=len(framing_polylines),
                    )
                else:
                    layer = primary.layer
                    primary_safety = evaluate_emission(
                        cfg=cfg,
                        power_percent=layer.power_percent,
                        frequency_khz=layer.frequency_khz,
                        pulse_width_ns=layer.pulse_width_ns,
                        intends_emission=True,
                        arm=bool(live.get("arm")),
                        confirm=str(live.get("confirm", "")),
                        operation=layer.operation,
                        paths_count=sum(len(job.polylines) for job in emitting),
                    )
                self.send_json(HTTPStatus.OK, {
                    "mode": "dry-run",
                    "emission_enabled": False,
                    "object_count": len(obj_list),
                    "layer_count": len(layers),
                    "emitting_layers": sum(1 for job in emitting if job.polylines),
                    "framing_paths": len(framing_polylines),
                    "emitting_paths": len(emitting_polylines),
                    "bbox": bbox,
                    "layers": [
                        {
                            "layer": job.layer.to_dict(),
                            "path_count": len(job.polylines),
                            "would_emit": job.layer.emits_when_marking() and bool(job.polylines),
                        }
                        for job in jobs
                    ],
                    "layer_errors": layer_errors,
                    "safety": primary_safety.to_dict(),
                    "frame_settings": {
                        "power_percent": params.power,
                        "frequency_khz": params.frequency_khz,
                        "pulse_width_ns": params.pulse_width_ns,
                        "speed_mm_s": params.mark_speed,
                    },
                })
                return

            if path == "/api/frame":
                if not machine.direct_control:
                    raise ValueError("direct framing is not enabled for this machine; export G-code and use a GRBL sender")
                FRAME_LOOP.stop()
                clear_stop_request()
                if framing_polylines:
                    self.send_json(HTTPStatus.OK, run_hardware_job("frame_polylines", cfg, params, framing_polylines))
                else:
                    self.send_json(HTTPStatus.OK, run_hardware_job("frame_box", cfg, params))
                return
            if path == "/api/frame/start":
                if not machine.direct_control:
                    raise ValueError("continuous framing is only available on the JCZ/LMC profile")
                self.send_json(HTTPStatus.OK, FRAME_LOOP.start(cfg, params, framing_polylines or None))
                return
            if path == "/api/frame/stop":
                self.send_json(HTTPStatus.OK, FRAME_LOOP.stop())
                return
            if path == "/api/mark":
                if not machine.direct_control:
                    raise ValueError("direct machine control is not enabled for this profile; export G-code instead")
                FRAME_LOOP.stop()
                clear_stop_request()
                repeat_count = max(1, min(999, int(live.get("repeat_count", 1) or 1)))
                layer_errors = validate_layers(layers, cfg)
                if layer_errors:
                    self.send_json(HTTPStatus.BAD_REQUEST, {
                        "error": "one or more layers have invalid parameters",
                        "layer_errors": layer_errors,
                    })
                    return
                results = []
                for repeat_index in range(repeat_count):
                    if stop_requested():
                        break
                    for job in emitting:
                        if stop_requested():
                            break
                        if not job.polylines:
                            continue
                        layer = job.layer
                        layer_params = JobParams(
                            text="",
                            power=layer.power_percent,
                            frequency_khz=layer.frequency_khz,
                            pulse_width_ns=layer.pulse_width_ns,
                            size_mm=10.0,
                            mark_speed=layer.speed_mm_s,
                        )
                        if layer.operation == "raster_engrave":
                            raster_result = hatch_polylines(
                                job.polylines,
                                angle_deg=layer.raster_angle_deg,
                                pitch_mm=layer.raster_pitch_mm,
                                passes=max(1, layer.passes),
                                angle_step_deg=layer.raster_angle_step_deg,
                            )
                            if not raster_result.segments:
                                results.append({
                                    "ok": True,
                                    "operation": "raster_skip",
                                    "layer": layer.layer_id,
                                    "repeat": repeat_index + 1,
                                    "reason": "no closed regions for raster fill",
                                    "skipped_open": raster_result.skipped_open_count,
                                })
                                continue
                            results.append(run_hardware_job(
                                "mark_polylines",
                                cfg,
                                layer_params,
                                raster_result.segments,
                                arm=bool(live.get("arm")),
                                confirm=str(live.get("confirm", "")),
                                operation=layer.operation,
                                timeout_s=600.0,
                            ))
                            results[-1].update({
                                "repeat": repeat_index + 1,
                                "raster": raster_result.to_dict(),
                            })
                        else:
                            for _ in range(max(1, layer.passes)):
                                if stop_requested():
                                    break
                                results.append(run_hardware_job(
                                    "mark_polylines",
                                    cfg,
                                    layer_params,
                                    job.polylines,
                                    arm=bool(live.get("arm")),
                                    confirm=str(live.get("confirm", "")),
                                    operation=layer.operation,
                                    timeout_s=600.0,
                                ))
                                results[-1].update({"repeat": repeat_index + 1})
                if not results:
                    # No layered emitting jobs — fall back to the live-panel single-pass.
                    if not emitting_polylines and not framing_polylines:
                        self.send_json(HTTPStatus.BAD_REQUEST, {"error": "no markable geometry in job"})
                        return
                    for repeat_index in range(repeat_count):
                        if stop_requested():
                            break
                        results.append(run_hardware_job(
                            "mark_polylines",
                            cfg,
                            params,
                            emitting_polylines or framing_polylines,
                            arm=bool(live.get("arm")),
                            confirm=str(live.get("confirm", "")),
                            timeout_s=600.0,
                        ))
                        results[-1].update({"repeat": repeat_index + 1})
                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "operation": "mark",
                    "emission_enabled": True,
                    "repeat_count": repeat_count,
                    "passes": [r for r in results],
                })
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except PermissionError as exc:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def job_params_from_payload(payload: dict) -> JobParams:
    return JobParams(
        text=str(payload.get("text", "")),
        power=float(payload.get("power", 1)),
        frequency_khz=float(payload.get("frequency_khz", 30)),
        pulse_width_ns=float(payload.get("pulse_width_ns", 200)),
        size_mm=float(payload.get("size_mm", 10)),
        mark_speed=float(payload.get("mark_speed", 1000)),
    )


def first_burn_params(kind: str) -> dict:
    return {
        "power": 1.0,
        "frequency_khz": 30.0,
        "pulse_width_ns": 200.0,
        "mark_speed": 1000.0,
        "kind": kind,
    }


def first_burn_polylines(kind: str) -> list[list[list[float]]]:
    if kind == "line":
        return [[[-2.5, 0.0], [2.5, 0.0]]]
    return [[[0.0, 0.0], [0.05, 0.0]]]


def browser_url(host: str, port: int) -> str:
    """Return a browser-safe local URL for the server's bind address."""
    if host in {"0.0.0.0", ""}:
        browser_host = "127.0.0.1"
    elif host == "::":
        browser_host = "[::1]"
    elif ":" in host and not host.startswith("["):
        browser_host = f"[{host}]"
    else:
        browser_host = host
    return f"http://{browser_host}:{port}"


def open_default_browser(url: str) -> bool:
    """Open a new tab in the user's configured default browser without failing startup."""
    try:
        return bool(webbrowser.open(url, new=2, autoraise=True))
    except Exception as exc:
        print(f"Could not open the default browser automatically: {exc}")
        return False


def serve(host: str, port: int, markcfg: Path, *, open_browser: bool = True) -> None:
    handler = type("ConfiguredUiHandler", (UiHandler,), {"markcfg": markcfg})
    server = ThreadingHTTPServer((host, port), handler)
    url = browser_url(host, port)
    print(f"OpenLaser UI: {url}  (safety: 0..100% power, ARM required)")
    print("Default behavior is dry-run / red-light framing only. Mark Job needs ARM and confirmation.")
    if open_browser:
        if open_default_browser(url):
            print("Opened OpenLaser in your default browser.")
        else:
            print(f"Open this address manually: {url}")
    try:
        server.serve_forever()
    finally:
        FRAME_LOOP.stop()
