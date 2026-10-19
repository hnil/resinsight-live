# Copyright 2026 SINTEF.
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP server exposing a running ResInsight instance over its rips/gRPC API.

Read-mostly: the session observes the model and what the user is doing in the GUI
(ri_watch / ri_selected_cells / ri_snapshot) and can drive the view when asked.
"""

from __future__ import annotations

import contextlib
import glob
import functools
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer, Image

import rips
import Commands_pb2

import render as _render

mcp = MCPServer(
    "resinsight-live",
    instructions=(
        "Drives a running ResInsight (OPM's open-source reservoir visualisation) via gRPC. "
        "Start ResInsight with the gRPC server enabled, then call ri_status. "
        "Use ri_watch to see what the user has changed in the GUI since the last call, "
        "ri_selected_cells for the cells they picked, ri_snapshot to look at a 3D view."
    ),
)

_S: Dict[str, Any] = {"inst": None, "port": None, "watch": None, "cellinfo": {}}


# --------------------------------------------------------------------------- connection


def _inst(port: int = 0) -> rips.Instance:
    if port or _S["inst"] is None:
        inst = _render.connect(port)
        _S["inst"] = inst
        _S["port"] = inst.port
    return _S["inst"]


def _project():
    return _inst().project


def _all_cases(proj) -> List[Any]:
    """Grid cases, and geomechanical cases when rips knows them."""
    cases = list(proj.cases())
    geomech = getattr(rips, "GeoMechCase", None)
    if geomech is not None:
        cases += proj.descendants(geomech)
    return cases


def _is_geomech(obj) -> bool:
    classes = getattr(rips, "GeoMechView", None), getattr(rips, "GeoMechCase", None)
    return any(c is not None and isinstance(obj, c) for c in classes)


def _time_step_labels(case) -> List[str]:
    """Dates as yyyy-mm-dd, or the step names of a geomechanical case (its steps need not be dates)."""
    steps = case.time_steps()
    if steps:
        return [f"{d.year:04d}-{d.month:02d}-{d.day:02d}" for d in steps]
    names = getattr(case, "time_step_names", None)
    return list(names()) if names else []


def _property_type(case, property_type: str) -> str:
    """Geomechanical results are addressed by position; default to element-nodal."""
    if _is_geomech(case) and not property_type.startswith("GEOMECH_"):
        return "GEOMECH_ELEMENT_NODAL"
    return property_type


def _case(case_id: int = -1):
    cases = _all_cases(_project())
    if not cases:
        raise ValueError("no case loaded in ResInsight")
    if case_id < 0:
        return cases[0]
    for c in cases:
        if c.id == case_id:
            return c
    raise ValueError(f"no case with id {case_id} (loaded: {[x.id for x in cases]})")


def _case_with_selection(case_id: int = -1):
    """The case the user picked in, when no id is given."""
    if case_id < 0:
        for c in _all_cases(_project()):
            try:
                if c.selected_cells():
                    return c
            except Exception:
                continue
    return _case(case_id)


def _view(view_id: int = -1):
    proj = _project()
    views = proj.views()
    if not views:
        raise ValueError("no 3D view open in ResInsight; use ri_create_view")
    if view_id < 0:
        return views[0]
    for v in views:
        if v.id == view_id:
            return v
    raise ValueError(f"no view with id {view_id} (open: {[v.id for v in views]})")


def _err(exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    if "Could not find any ResInsight" in str(exc) or "closed" in str(exc).lower():
        _S["inst"] = None
        msg += (
            "\nNo connection. Start ResInsight built with gRPC "
            "(Preferences > Scripting: enable gRPC server), then retry."
        )
    return msg


def _guard(fn):
    # functools.wraps keeps the real signature: the tool schema is built from it
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return _err(exc)

    return wrapper


# --------------------------------------------------------------------------- path allowlist

# Claude's sandbox does not apply to MCP servers, so the server enforces its own limit.
ALLOWED_DIRS_ENV = "RESINSIGHT_LIVE_ALLOWED_DIRS"
OLD_ALLOWED_DIRS_ENV = "RESINSIGHT_MCP_ALLOWED_DIRS"  # read for one release after the rename

# fields of ResInsight commands that name files or folders; wellPath* fields are well names
_COMMAND_PATH_FIELDS = {
    "createGridCaseGroup": ["casePaths"],
    "exportContourMapToText": ["exportFileName"],
    "exportFlowCharacteristics": ["fileName"],
    "exportMultiCaseSnapshots": ["gridListFile"],
    "exportProperty": ["exportFile"],
    "exportSnapshots": ["exportFolder"],
    "exportWellLogPlotData": ["exportFolder"],
    "importFormationNames": ["formationFiles"],
    "importWellLogFiles": ["wellLogFiles", "wellLogFolder"],
    "importWellPaths": ["wellPathFiles", "wellPathFolder"],
    "loadCase": ["path"],
    "openProject": ["path"],
    "replaceCase": ["newGridFile"],
    "replaceMultipleCases": ["casePairs.newGridFile"],
    "replaceSourceCases": ["gridListFile"],
    "saveProject": ["filePath"],
    "setExportFolder": ["path"],
    "setStartDir": ["path"],
}
_NOT_PATHS = {"wellPath", "wellPathNames", "filePrefix", "customFileName"}
# the field holding an export's own target; without it the export goes to the global export folder
_EXPORT_TARGET = {
    "exportContourMapToText": "exportFileName",
    "exportFlowCharacteristics": "fileName",
    "exportProperty": "exportFile",
    "exportSnapshots": "exportFolder",
    "exportWellLogPlotData": "exportFolder",
}
_BLOCKED_COMMANDS = {
    "runOctaveScript": "it runs arbitrary Octave code",
    "closeProject": "this server did not start this ResInsight; close it from the GUI",
}
_PATHISH = re.compile(r"path|file|folder|dir", re.IGNORECASE)


def _allowed_dirs() -> List[Path]:
    raw = os.environ.get(ALLOWED_DIRS_ENV) or os.environ.get(OLD_ALLOWED_DIRS_ENV, "")
    return [Path(os.path.expanduser(d.strip())).resolve() for d in raw.split(os.pathsep) if d.strip()]


def _check_path(path: str, what: str) -> None:
    """Refuse a path outside RESINSIGHT_LIVE_ALLOWED_DIRS; no limit when that is unset."""
    roots = _allowed_dirs()
    if not roots:
        return
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        raise PermissionError(f"{what}: '{path}' must be an absolute path")
    real = p.resolve()  # follows symlinks, so a link cannot lead out of an allowed directory
    if not any(real == r or r in real.parents for r in roots):
        raise PermissionError(
            f"{what}: '{path}' is outside the allowed directories {[str(r) for r in roots]}"
        )


def _string_values(obj: Any, keys: List[str]) -> List[str]:
    if isinstance(obj, list):
        return [v for o in obj for v in _string_values(o, keys)]
    if not keys:
        return [obj] if isinstance(obj, str) else []
    if isinstance(obj, dict) and keys[0] in obj:
        return _string_values(obj[keys[0]], keys[1:])
    return []


def _pathlike_fields(desc, prefix: str = "") -> List[str]:
    from google.protobuf.descriptor import FieldDescriptor

    found = []
    for f in desc.fields:
        if f.type == FieldDescriptor.TYPE_STRING and _PATHISH.search(f.name):
            found.append(prefix + f.name)
        elif f.type == FieldDescriptor.TYPE_MESSAGE:
            found += _pathlike_fields(f.message_type, f"{prefix}{f.name}.")
    return found


def _check_command(name: str, params: Dict[str, Any], desc) -> None:
    if name in _BLOCKED_COMMANDS:
        raise PermissionError(f"{name} is disabled: {_BLOCKED_COMMANDS[name]}")
    if not _allowed_dirs():
        return
    vetted = set(_COMMAND_PATH_FIELDS.get(name, []))
    unvetted = [f for f in _pathlike_fields(desc) if f not in vetted and f.split(".")[-1] not in _NOT_PATHS]
    if unvetted:
        # fail closed for commands added to ResInsight after this list was written
        raise PermissionError(f"{name}: fields {unvetted} are not vetted for the directory limit")
    for field in vetted:
        for value in _string_values(params, field.split(".")):
            if value:
                _check_path(value, f"{name}.{field}")
    custom = params.get("customFileName", "")
    if custom and os.path.basename(custom) != custom:
        raise PermissionError(f"{name}: customFileName must be a bare file name")
    if name == "saveProject" and not params.get("filePath"):
        raise PermissionError("saveProject: give filePath; saving in place may write outside the allowed directories")
    target = _EXPORT_TARGET.get(name)
    if name.startswith("export") and not (target and params.get(target)) and not _S.get("export_folder_ok"):
        raise PermissionError(
            f"{name} writes to ResInsight's export folder: first call setExportFolder with an "
            "allowed directory, or pass an explicit output path"
        )


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str)


# --------------------------------------------------------------------------- state


def _cell_result_of(view) -> Dict[str, Any]:
    try:
        cr = view.cell_result()
    except Exception:
        return {}
    if cr is None:
        return {}
    if _is_geomech(view):
        return {
            "result_position": getattr(cr, "result_position_type", None),
            "result_field": getattr(cr, "result_field_name", None),
            "result_component": getattr(cr, "result_component_name", None),
        }
    return {
        "result_type": getattr(cr, "result_type", None),
        "result_variable": getattr(cr, "result_variable", None),
    }


def _view_state(view) -> Dict[str, Any]:
    st: Dict[str, Any] = {"id": view.id, "class": type(view).__name__}
    for attr in (
        "current_time_step",
        "grid_z_scale",
        "background_color",
        "perspective_projection",
        "show_grid_box",
        "camera_point_of_interest",
    ):
        val = getattr(view, attr, None)
        if val is not None:
            st[attr] = val
    st.update(_cell_result_of(view))
    try:
        case = view.case()
        st["case_id"] = case.id
        st["case_name"] = case.name
        labels = _time_step_labels(case)
        i = st.get("current_time_step", 0)
        if 0 <= i < len(labels):
            st["time_step_name" if _is_geomech(view) else "date"] = labels[i]
        st["time_step_count"] = len(labels)
    except Exception:
        pass
    return st


def _snapshot_state() -> Dict[str, Any]:
    proj = _project()
    cases = {}
    for c in _all_cases(proj):
        try:
            cases[str(c.id)] = {
                "name": c.name,
                "class": type(c).__name__,
                "time_steps": len(_time_step_labels(c)),
                "selected_cells": [
                    [s.grid_index, s.ijk.i, s.ijk.j, s.ijk.k] for s in c.selected_cells()
                ],
            }
        except Exception as exc:  # a case can be half-loaded
            cases[str(c.id)] = {"name": getattr(c, "name", "?"), "error": str(exc)}
    return {
        "time": time.time(),
        "project": getattr(proj, "project_file_path", None) or "",
        "cases": cases,
        "views": {str(v.id): _view_state(v) for v in proj.views()},
        "plots": {str(p.id): type(p).__name__ for p in proj.plots()},
    }


def _diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if old.get("project") != new.get("project"):
        out.append(f"project: {old.get('project') or '(none)'} -> {new.get('project') or '(none)'}")

    for cid in set(old["cases"]) | set(new["cases"]):
        o, n = old["cases"].get(cid), new["cases"].get(cid)
        if o is None:
            out.append(f"case {cid} opened: {n.get('name')}")
            continue
        if n is None:
            out.append(f"case {cid} closed: {o.get('name')}")
            continue
        if o.get("selected_cells") != n.get("selected_cells"):
            sel = n.get("selected_cells") or []
            shown = ", ".join(f"grid{g} ijk({i+1},{j+1},{k+1})" for g, i, j, k in sel[:8])
            out.append(
                f"case {cid} ({n.get('name')}) selection: {len(sel)} cell(s)"
                + (f": {shown}{' ...' if len(sel) > 8 else ''}" if sel else " (cleared)")
            )

    for vid in set(old["views"]) | set(new["views"]):
        o, n = old["views"].get(vid), new["views"].get(vid)
        if o is None:
            out.append(f"view {vid} created on case {n.get('case_id')} ({n.get('case_name')})")
            continue
        if n is None:
            out.append(f"view {vid} closed (case {o.get('case_name')})")
            continue
        if o.get("current_time_step") != n.get("current_time_step"):
            out.append(
                f"view {vid} time step: {o.get('current_time_step')} -> "
                f"{n.get('current_time_step')} ({n.get('date')})"
            )
        if (o.get("result_type"), o.get("result_variable")) != (
            n.get("result_type"),
            n.get("result_variable"),
        ):
            out.append(
                f"view {vid} cell result: {o.get('result_variable')} "
                f"-> {n.get('result_variable')} [{n.get('result_type')}]"
            )
        if o.get("camera_point_of_interest") != n.get("camera_point_of_interest"):
            out.append(f"view {vid} camera moved to {n.get('camera_point_of_interest')}")
        for attr in ("grid_z_scale", "background_color"):
            if o.get(attr) != n.get(attr):
                out.append(f"view {vid} {attr}: {o.get(attr)} -> {n.get(attr)}")

    for pid in set(old["plots"]) | set(new["plots"]):
        if pid not in old["plots"]:
            out.append(f"plot {pid} created: {new['plots'][pid]}")
        elif pid not in new["plots"]:
            out.append(f"plot {pid} closed: {old['plots'][pid]}")
    return out


# --------------------------------------------------------------------------- tools


@mcp.tool()
@_guard
def ri_status(port: int = 0) -> str:
    """Connect to (or check) the running ResInsight instance and summarise the project.

    port: 0 = $RESINSIGHT_GRPC_PORT, else the one ResInsight answering on 50051-50070
    (several answering is an error listing them); otherwise use this port.
    owned is always false: this server never starts, closes or resizes the instance it drives.
    """
    inst = _inst(port)
    proj = inst.project
    return _dumps(
        {
            "port": _S["port"],
            "owned": False,
            "resinsight_version": inst.version_string(),
            "client_version": inst.client_version_string(),
            "project_file": getattr(proj, "project_file_path", ""),
            "cases": [
                {"id": c.id, "name": c.name, "type": type(c).__name__} for c in proj.cases()
            ],
            "views": [
                {"id": v.id, "case_id": getattr(v.case(), "id", None), "type": type(v).__name__}
                for v in proj.views()
            ],
            "plots": [{"id": p.id, "type": type(p).__name__} for p in proj.plots()],
        }
    )


@mcp.tool()
@_guard
def ri_overview() -> str:
    """Full picture of the loaded model: cases, grid sizes, time steps, views and their results."""
    proj = _project()
    cases = []
    for c in _all_cases(proj):
        entry: Dict[str, Any] = {"id": c.id, "name": c.name, "type": type(c).__name__}
        try:
            grids = c.grids()
            entry["grids"] = [
                {"index": i, "dimensions": [g.dimensions().i, g.dimensions().j, g.dimensions().k]}
                for i, g in enumerate(grids)
            ]
            entry["active_cells"] = c.cell_count().active_cell_count
            entry["reservoir_cells"] = c.cell_count().reservoir_cell_count
            labels = _time_step_labels(c)
            entry["time_steps"] = len(labels)
            if labels:
                entry["first_step"] = labels[0]
                entry["last_step"] = labels[-1]
        except Exception as exc:
            entry["warning"] = str(exc)
        cases.append(entry)
    return _dumps(
        {
            "project_file": getattr(proj, "project_file_path", ""),
            "cases": cases,
            "views": [_view_state(v) for v in proj.views()],
            "well_paths": [getattr(w, "name", "") for w in proj.well_paths()],
        }
    )


@mcp.tool()
@_guard
def ri_watch(reset: bool = False) -> str:
    """What the user changed in ResInsight since the last ri_watch call.

    Reports time-step changes, result switches, camera moves, cell selection,
    opened/closed cases, views and plots. First call (or reset=True) just takes a baseline.
    """
    new = _snapshot_state()
    old = _S.get("watch")
    _S["watch"] = new
    if old is None or reset:
        return "Baseline taken. Current state:\n" + _dumps(
            {"views": new["views"], "cases": new["cases"]}
        )
    changes = _diff(old, new)
    if not changes:
        return f"No changes in the last {new['time'] - old['time']:.0f} s."
    return "Changes since last check:\n- " + "\n- ".join(changes)


@mcp.tool()
@_guard
def ri_view(view_id: int = -1) -> str:
    """Detailed state of one 3D view (view_id 0 = first view): result shown, time step, camera."""
    return _dumps(_view_state(_view(view_id)))


@mcp.tool()
@_guard
def ri_selected_cells(case_id: int = -1, with_values: bool = True) -> str:
    """Cells the user has picked in the 3D view, with the value of the currently displayed result.

    ijk are reported 1-based, as shown in the ResInsight GUI.
    """
    case = _case_with_selection(case_id)
    cells = case.selected_cells()
    out: Dict[str, Any] = {
        "case": case.name,
        "count": len(cells),
        "cells": [
            {"grid": c.grid_index, "i": c.ijk.i + 1, "j": c.ijk.j + 1, "k": c.ijk.k + 1}
            for c in cells
        ],
    }
    if with_values and cells:
        view = None
        for v in _project().views():
            try:
                if v.case().id == case.id:
                    view = v
                    break
            except Exception:
                continue
        if view is not None:
            cr = _cell_result_of(view)
            rtype, rvar = cr.get("result_type"), cr.get("result_variable")
            if rtype and rvar and rvar != "None":
                step = getattr(view, "current_time_step", 0)
                vals = case.selected_cell_property(rtype, rvar, step)
                out["result"] = {"type": rtype, "variable": rvar, "time_step": step}
                for cell, val in zip(out["cells"], vals):
                    cell["value"] = val
    return _dumps(out)


@mcp.tool()
@_guard
def ri_picked_cell_report(case_id: int = -1, properties: Optional[List[str]] = None) -> str:
    """Every property value in the cell the user picked in the 3D view.

    Static properties are read at step 0, dynamic ones at the view's current time step.
    Pass `properties` to restrict the list; default is everything the case offers.
    """
    case = _case_with_selection(case_id)
    sel = case.selected_cells()
    if not sel:
        return "No cell is picked in the 3D view — click a cell in ResInsight first."
    view = next((v for v in _project().views() if v.case().id == case.id), None)
    step = getattr(view, "current_time_step", 0) if view else 0
    steps = case.time_steps()
    d = steps[step] if step < len(steps) else None
    out: Dict[str, Any] = {
        "case": case.name,
        "cells": [
            {"grid": c.grid_index, "i": c.ijk.i + 1, "j": c.ijk.j + 1, "k": c.ijk.k + 1}
            for c in sel
        ],
        "time_step": step,
        "date": f"{d.year:04d}-{d.month:02d}-{d.day:02d}" if d else None,
    }
    for ptype, at_step in (("STATIC_NATIVE", 0), ("DYNAMIC_NATIVE", step)):
        vals: Dict[str, Any] = {}
        names = properties or sorted(case.available_properties(ptype))
        for name in names:
            try:
                v = case.selected_cell_property(ptype, name, at_step)
                vals[name] = list(v) if len(sel) > 1 else v[0]
            except Exception:
                continue
        out[ptype.lower()] = vals
    return _dumps(out)


@mcp.tool()
@_guard
def ri_snapshot(view_id: int = -1) -> Image:
    """Export a PNG snapshot of a 3D view and return it, so the session can see the model."""
    view = _view(view_id)
    folder = tempfile.mkdtemp(prefix="ri_mcp_snap_")
    view.export_snapshot(prefix="view", export_folder=folder)
    files = sorted(glob.glob(os.path.join(folder, "*.png")), key=os.path.getmtime)
    if not files:
        raise RuntimeError(f"ResInsight wrote no snapshot to {folder}")
    return Image(path=files[-1])


@mcp.tool()
@_guard
def ri_properties(case_id: int = -1, property_type: str = "DYNAMIC_NATIVE") -> str:
    """List result properties available on a case.

    property_type: DYNAMIC_NATIVE | STATIC_NATIVE | GENERATED | INPUT_PROPERTY | SOURSIMRL |
    FORMATION_NAMES | FLOW_DIAGNOSTICS | INJECTION_FLOODING; for geomechanical cases the position
    GEOMECH_NODAL | GEOMECH_ELEMENT_NODAL (default) | GEOMECH_INTEGRATION_POINT | GEOMECH_ELEMENT,
    with names "FIELD:COMPONENT" (e.g. "SE:S1", "POR-Bar").
    """
    case = _case(case_id)
    return _dumps(sorted(case.available_properties(_property_type(case, property_type))))


@mcp.tool()
@_guard
def ri_time_steps(case_id: int = -1) -> str:
    """Time steps of a case as index -> date (step name for geomechanical cases)."""
    return _dumps(dict(enumerate(_time_step_labels(_case(case_id)))))


@mcp.tool()
@_guard
def ri_property_stats(
    property_name: str,
    case_id: int = -1,
    property_type: str = "DYNAMIC_NATIVE",
    time_step: int = 0,
) -> str:
    """Statistics (min/max/mean/percentiles) of a property over the active cells at one time step.

    Use this instead of pulling whole arrays: it is one number set per call. Geomechanical cases:
    one value per element (the mean over its nodes for nodal and element-nodal results); see
    ri_properties for the property types and names.
    """
    case = _case(case_id)
    property_type = _property_type(case, property_type)
    vals = [v for v in case.active_cell_property(property_type, property_name, time_step)]
    finite = [v for v in vals if v == v and abs(v) < 1e29]
    if not finite:
        return _dumps({"property": property_name, "count": len(vals), "note": "no finite values"})
    finite_sorted = sorted(finite)

    def pct(p: float) -> float:
        return finite_sorted[min(len(finite_sorted) - 1, int(p * len(finite_sorted)))]

    return _dumps(
        {
            "property": property_name,
            "type": property_type,
            "time_step": time_step,
            "count": len(vals),
            "finite": len(finite),
            "min": finite_sorted[0],
            "p10": pct(0.10),
            "median": pct(0.50),
            "p90": pct(0.90),
            "max": finite_sorted[-1],
            "mean": statistics.fmean(finite),
        }
    )


def _cell_index_map(case) -> Dict[tuple, int]:
    key = case.id
    cached = _S["cellinfo"].get(key)
    if cached is None:
        cached = {}
        for idx, info in enumerate(case.cell_info_for_active_cells()):
            cached[(info.grid_index, info.local_ijk.i, info.local_ijk.j, info.local_ijk.k)] = idx
        _S["cellinfo"][key] = cached
    return cached


@mcp.tool()
@_guard
def ri_cell_values(
    property_name: str,
    cells: List[List[int]],
    case_id: int = -1,
    property_type: str = "DYNAMIC_NATIVE",
    time_step: int = 0,
    grid_index: int = 0,
) -> str:
    """Value of a property in specific cells. cells = [[i,j,k], ...], 1-based as in the GUI.

    Geomechanical cases: grid_index is the part, values per element as in ri_property_stats.
    """
    case = _case(case_id)
    property_type = _property_type(case, property_type)
    imap = _cell_index_map(case)
    vals = case.active_cell_property(property_type, property_name, time_step)
    out = []
    for ijk in cells:
        i, j, k = (int(x) - 1 for x in ijk[:3])
        idx = imap.get((grid_index, i, j, k))
        out.append(
            {
                "i": i + 1,
                "j": j + 1,
                "k": k + 1,
                "value": vals[idx] if idx is not None and idx < len(vals) else None,
                "active": idx is not None,
            }
        )
    return _dumps({"property": property_name, "time_step": time_step, "cells": out})


@mcp.tool()
@_guard
def ri_wells(case_id: int = -1) -> str:
    """Simulation wells in a case and well paths in the project."""
    case = _case(case_id)
    return _dumps(
        {
            "simulation_wells": [w.name for w in case.simulation_wells()],
            "well_paths": [getattr(w, "name", "") for w in _project().well_paths()],
        }
    )


@mcp.tool()
@_guard
def ri_set_time_step(time_step: int, view_id: int = -1) -> str:
    """Move a 3D view to a time step (the user sees this happen)."""
    view = _view(view_id)
    view.set_time_step(time_step)
    return _dumps(_view_state(_view(view.id)))


@mcp.tool()
@_guard
def ri_set_cell_result(
    result_variable: str, view_id: int = -1, result_type: str = "DYNAMIC_NATIVE", component: str = ""
) -> str:
    """Show a different property in a 3D view (the user sees this happen).

    Geomechanical views: result_type is the position (NODAL, ELEMENT_NODAL, INTEGRATION_POINT,
    ELEMENT; anything else means ELEMENT_NODAL), result_variable the field (SE, ST, S, E, NE,
    POR-Bar, U, S_VTK, ...) and component e.g. S1, S11, U3; "SE:S1" works too.
    """
    view = _view(view_id)
    if _is_geomech(view):
        field, _, comp = result_variable.partition(":")
        cr = view.cell_result()
        if cr is None or not hasattr(cr, "result_field_name"):
            raise ValueError("this ResInsight cannot script geomechanical results (needs a 2026.9+ build)")
        positions = ("NODAL", "ELEMENT_NODAL", "INTEGRATION_POINT", "ELEMENT")
        cr.result_position_type = result_type if result_type in positions else "ELEMENT_NODAL"
        cr.result_field_name = field
        cr.result_component_name = component or comp
        cr.update()
    else:
        view.apply_cell_result(result_type, result_variable)
    return _dumps(_view_state(_view(view.id)))


def _run_info(path: str) -> Optional[Dict[str, Any]]:
    """The run.json (opm-agentic provenance) of a run directory or of the dir holding path."""
    p = Path(path).expanduser()
    f = (p if p.is_dir() else p.parent) / "run.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


@mcp.tool()
@_guard
def ri_open(path: str, create_view: bool = True) -> str:
    """Open a ResInsight project (.rsp), or load a case (.EGRID/.GRID, a run directory, a
    .GRDECL grid with its properties, or a geomechanical case: .pvd from OPM Flow's VTK output,
    .odb, .inp)."""
    _check_path(path, "ri_open")
    proj = _project()
    _S["cellinfo"].clear()
    if path.lower().endswith(".rsp"):
        proj.open(path)
        return ri_status()
    run = _run_info(path)
    if Path(path).is_dir():
        path = str(_render.find_grid(path))
    case = proj.load_case(path)
    if case is None:
        raise ValueError(f"ResInsight loaded {path}, but this rips cannot return the case (needs a 2026.9+ build)")
    out = {"loaded": path, "case_id": case.id, "name": case.name, "type": type(case).__name__}
    if run:
        out["run"] = run
    if create_view:
        # a geomechanical case opens with a view of its own
        views = case.descendants(rips.View) if _is_geomech(case) else []
        out["view_id"] = views[0].id if views else case.create_view().id
    return _dumps(out)


@mcp.tool()
@_guard
def ri_create_view(case_id: int = -1) -> str:
    """Create a new 3D view on a case (needed before ri_snapshot if no view is open)."""
    return _dumps(_view_state(_case(case_id).create_view()))


def _slice_filter_xml(name: str, rng: Dict[str, tuple]) -> str:
    return (
        "<CellRangeFilter><Name>" + name + "</Name><IsChecked>True</IsChecked>"
        "<FilterType>INCLUDE</FilterType><GridIndex>0</GridIndex>"
        + "".join(
            f"<StartIndex{a.upper()}>{rng[a][0]}</StartIndex{a.upper()}><CellCount{a.upper()}>{rng[a][1]}</CellCount{a.upper()}>"
            for a in "ijk"
        )
        + "</CellRangeFilter>"
    )


@mcp.tool()
@_guard
def ri_slice(slice_spec: str, view_id: int = -1, face_camera: bool = True) -> str:
    """Show only an i, j or k slice in a live 3D view: 'j=44' or 'k=10:14' (1-based).

    Replaces the view's range filters. A range filter made over gRPC is not tied to its case and
    shows nothing, so the project is saved to a temporary file, the filter written into the view
    there, and the project reopened; every view keeps its result and time step. Afterwards the
    project lives in that temporary file; save it elsewhere from the GUI to keep it.
    face_camera turns the camera square to the slice, from the grid's own orientation.
    """
    view = _view(view_id)
    if _is_geomech(view):
        raise ValueError("ri_slice works on grid cases; use ri_render with slice_spec for geomechanical cases")
    axis, start, count = _render.parse_slice(slice_spec)
    case = view.case()
    ni, nj, nk = _render.grid_dimensions(case, False)
    rng = {"i": (1, ni), "j": (1, nj), "k": (1, nk)}
    rng[axis] = (start, count)
    distance = -view.camera_matrix[11]

    # Mark the view's filter collection, so the view can be found in the saved project.
    marker = f"__ri_slice_{os.getpid()}_{int(time.time())}__"
    coll = view.descendants(rips.generated.generated_classes.CellFilterCollection)[0]
    tag = coll.add_new_object(rips.generated.generated_classes.CellRangeFilter)
    tag.name = marker
    tag.is_checked = False
    tag.update()

    proj = _project()
    rsp = Path(tempfile.mkdtemp(prefix="ri_slice_")) / "sliced.rsp"
    proj.save(str(rsp))
    text = rsp.read_text()
    m = text.find(f"<Name>{marker}</Name>")
    a = text.rfind("<CellFilters>", 0, m)
    b = text.find("</CellFilters>", m) + len("</CellFilters>")
    if m < 0 or a < 0:
        raise RuntimeError("could not find the view in the saved project")
    text = text[:a] + "<CellFilters>" + _slice_filter_xml(f"slice {slice_spec}", rng) + "</CellFilters>" + text[b:]
    if face_camera:
        # Without a stored camera ResInsight frames the model on opening the view.
        v0 = max(text.rfind("<EclipseView>", 0, a), text.rfind("<EclipseView ", 0, a))
        head = re.sub(r"<CameraPosition>[^<]*</CameraPosition>|<CameraPointOfInterest>[^<]*</CameraPointOfInterest>",
                      "", text[v0:a])
        text = text[:v0] + head + text[a:]
    rsp.write_text(text)
    proj.open(str(rsp))
    _S["cellinfo"].clear()
    if face_camera:
        target = _view(view.id)
        frame = _render.slice_frame(case, axis, start, target.grid_z_scale)
        if frame:
            target.camera_matrix = _render.look_at_point(
                _render.slice_direction(case, axis, start), frame[0], frame[1])
            target.camera_point_of_interest = frame[0]
            target.update()
    out = _view_state(_view(view.id))
    out["project_file"] = str(rsp)
    return _dumps(out)


@mcp.tool()
@_guard
def ri_camera(direction: str, view_id: int = -1) -> str:
    """Point a live 3D view along a direction: top, front (looking north), side (looking east),
    oblique, or 'x,y,z' with x east, y north, z up. The distance to the model is kept."""
    view = _view(view_id)
    view.camera_matrix = _render.look_at_point(
        _render.camera_direction(direction), view.camera_point_of_interest, -view.camera_matrix[11])
    view.update()
    return _dumps(_view_state(_view(view.id)))


def _summary_case(summary_case_id: int = -1):
    cases = _project().summary_cases()
    if not cases:
        raise ValueError("no summary case loaded (open a case with .SMSPEC/.UNSMRY beside it)")
    if summary_case_id < 0:
        return cases[0]
    for c in cases:
        if c.id == summary_case_id:
            return c
    raise ValueError(f"no summary case {summary_case_id} (loaded: {[c.id for c in cases]})")


@mcp.tool()
@_guard
def ri_summary_cases() -> str:
    """Summary (.SMSPEC) cases loaded, with the vectors each one offers."""
    out = []
    for c in _project().summary_cases():
        addrs = list(c.available_addresses().values)
        out.append(
            {
                "id": c.id,
                "name": getattr(c, "short_name", ""),
                "file": getattr(c, "summary_header_filename", ""),
                "vector_count": len(addrs),
                "wells": sorted({a.split(":")[1] for a in addrs if a.startswith("W") and ":" in a}),
            }
        )
    return _dumps(out)


@mcp.tool()
@_guard
def ri_summary_values(address: str, summary_case_id: int = -1) -> str:
    """Values of one summary vector, e.g. 'WBHP:B-3H' or 'FOPR'."""
    c = _summary_case(summary_case_id)
    return _dumps({"address": address, "values": list(c.summary_vector_values(address).values)})


@mcp.tool()
@_guard
def ri_summary_plot(address: str, summary_case_id: int = -1) -> str:
    """Open a summary plot in ResInsight's plot window, e.g. address='WBHP:B-3H'.

    The user sees the plot appear; the values are returned as well.
    """
    c = _summary_case(summary_case_id)
    coll = _project().descendants(rips.SummaryPlotCollection)
    if not coll:
        raise RuntimeError("no SummaryPlotCollection in the project")
    coll[0].new_summary_plot(summary_cases=[c], address=address)
    return _dumps(
        {
            "plotted": address,
            "summary_case": getattr(c, "short_name", ""),
            "values": list(c.summary_vector_values(address).values),
        }
    )


@mcp.tool()
@_guard
def ri_render(
    case: str,
    properties: str = "",
    steps: str = "first,last",
    slice_spec: str = "",
    camera: str = "",
    zscale: float = 0.0,
    vectors: str = "",
    template: str = "",
    out: str = "",
    max_images: int = 6,
    list_only: bool = False,
    geomech: bool = False,
):
    """Pictures of a simulation run, rendered by a private ResInsight that is closed afterwards.

    The user's interactive ResInsight is not touched. case: run dir, .DATA or .EGRID; a run.json
    beside it is reported and copied to out/source_run.json. Geomechanical results: case = the
    .pvd of OPM Flow's VTK output, or geomech=True to take the one beside the grid; properties
    'SE:S1,ST:S33,POR-Bar,U:U3' (position found automatically, or 'NODAL/POR-Bar').
    properties: 'TEMP,PRESSURE'; steps: 'first,last', 'all' or '0,5,last'.
    slice_spec: 'j=6' or 'k=10:14' (1-based) to see inside the grid; camera: top|front|side|oblique
    (default follows the slice). vectors: summary plots, 'WBHP:B-3H,FOPR'. template: an .rsp
    whose views (filters, camera, colours) are reused. Call with list_only=True first to see
    steps, properties and wells. Images come back inline (up to max_images); all paths listed.
    """
    import datetime
    import tempfile

    _check_path(case, "ri_render case")
    for value, what in ((out, "ri_render out"), (template, "ri_render template")):
        if value:
            _check_path(value, what)
    # rips prints to stdout, which is the MCP channel under stdio
    if list_only:
        with contextlib.redirect_stdout(sys.stderr):
            return _render.render(case, list_only=True, geomech=geomech)
    if not out:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = str(Path(tempfile.gettempdir()) / "resinsight_renders" / f"{Path(case).stem}_{stamp}")
    with contextlib.redirect_stdout(sys.stderr):
        files = _render.render(
            case, out, properties, steps, vectors, slice_spec, camera, zscale, template, geomech=geomech
        )
    listing = f"{len(files)} image(s) in {out}:\n" + "\n".join(f.name for f in files)
    run = _run_info(case)
    if run:
        (Path(out) / "source_run.json").write_text(json.dumps(run, indent=2) + "\n")
        listing = f"run: {json.dumps(run)}\n" + listing
    return [listing, *(Image(path=str(f)) for f in files[:max_images])]


@mcp.tool()
@_guard
def ri_execute_command(name: str = "", params: Optional[Dict[str, Any]] = None) -> str:
    """Escape hatch to ResInsight's command API. Call with no name to list available commands.

    runOctaveScript is disabled. With RESINSIGHT_LIVE_ALLOWED_DIRS set, every file or folder
    argument must lie inside those directories, and exports need an explicit target or an
    export folder set through setExportFolder first.
    """
    fields = Commands_pb2.CommandParams.DESCRIPTOR.fields_by_name
    if not name:
        return _dumps(sorted(fields))
    if name not in fields:
        raise ValueError(f"unknown command '{name}'; call with no name to list commands")
    params = params or {}
    _check_command(name, params, fields[name].message_type)
    msg_cls = getattr(Commands_pb2, fields[name].message_type.name)
    reply = _project()._execute_command(**{name: msg_cls(**params)})
    if name == "setExportFolder":
        _S["export_folder_ok"] = True  # only reached when the folder passed _check_command
    return _dumps({"command": name, "reply": str(reply)})


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="MCP server for a running ResInsight")
    ap.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1", help="bind address for --http")
    ap.add_argument("--port", type=int, default=8765, help="port for --http")
    ap.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a --host other than loopback; the server has no authentication",
    )
    args = ap.parse_args()
    if args.http and args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        # anyone who reaches the port could drive ResInsight and read files
        ap.error(f"--host {args.host} would expose an unauthenticated server; add --allow-remote to insist")

    if args.http:
        # stateless: each client keeps its own session, so several can attach at once
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run()
