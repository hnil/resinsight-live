# Copyright 2026 SINTEF.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Batch pictures of a reservoir run with ResInsight: 3D property snapshots and summary plots."""

import argparse
import contextlib
import math
import os
import re
import shutil
import socket
import sys
from pathlib import Path

import grpc
import rips
import App_pb2_grpc
import Commands_pb2 as Cmd  # importable once rips has put its generated dir on sys.path
from Definitions_pb2 import Empty

SCAN_PORTS = range(50051, 50071)  # what rips.Instance.find() and other clients scan


def _answers(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        if s.connect_ex(("localhost", port)) != 0:
            return False
    channel = grpc.insecure_channel(f"localhost:{port}", options=[("grpc.enable_http_proxy", False)])
    try:
        App_pb2_grpc.AppStub(channel).GetVersion(Empty(), timeout=1)
        return True
    except grpc.RpcError:
        return False
    finally:
        channel.close()


def find_ports() -> list[int]:
    """Ports in SCAN_PORTS where a ResInsight answers."""
    return [p for p in SCAN_PORTS if _answers(p)]


def connect(port: int = 0) -> rips.Instance:
    """Attach to the given port, else $RESINSIGHT_GRPC_PORT, else the one ResInsight that answers.

    Never guesses between several instances: one may belong to another server.
    """
    port = port or int(os.environ.get("RESINSIGHT_GRPC_PORT") or 0)
    if not port:
        ports = find_ports()
        if not ports:
            raise RuntimeError(f"Could not find any ResInsight on ports {SCAN_PORTS.start}-{SCAN_PORTS.stop - 1}")
        if len(ports) > 1:
            raise RuntimeError(
                f"several ResInsight instances answer, on ports {ports}; pick one with "
                "ri_status(port=...) or RESINSIGHT_GRPC_PORT"
            )
        port = ports[0]
    return rips.Instance(port=port)


def private_port() -> int:
    """A free port outside SCAN_PORTS, so other clients' scans never attach to our instance."""
    while True:
        with socket.socket() as s:
            s.bind(("localhost", 0))
            port = s.getsockname()[1]
        if port not in SCAN_PORTS:
            return port


def default_executable() -> str:
    candidates = [
        os.environ.get("RESINSIGHT_EXECUTABLE", ""),
        "/Applications/ResInsight.app/Contents/MacOS/ResInsight",
        str(Path.home() / "Applications/ResInsight.app/Contents/MacOS/ResInsight"),
        shutil.which("ResInsight") or "",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), "")

CAMERAS = {
    "top": (0.0, 0.0, -1.0),
    "front": (0.0, 1.0, 0.0),  # looking north, shows a j-slice face-on
    "side": (1.0, 0.0, 0.0),  # looking east, shows an i-slice face-on
    "oblique": (1.0, 1.2, -0.8),
}
SLICE_CAMERA = {"i": "side", "j": "front", "k": "top"}


def find_grid(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        grids = sorted(p.glob("*.EGRID")) or sorted(p.glob("*.GRID"))
        if len(grids) != 1:
            raise ValueError(f"{p}: expected exactly one .EGRID, found {[g.name for g in grids]}")
        return grids[0]
    for suffix in (".EGRID", ".GRID"):
        cand = p.with_suffix(suffix)
        if cand.exists():
            return cand
    raise ValueError(f"no .EGRID/.GRID next to {p}")


def parse_steps(spec: str, n: int) -> list[int]:
    if spec == "all":
        return list(range(n))
    steps = set()
    for tok in spec.split(","):
        tok = tok.strip()
        s = 0 if tok == "first" else n - 1 if tok == "last" else int(tok)
        steps.add(s if s >= 0 else n + s)
    return sorted(s for s in steps if 0 <= s < n)


def parse_slice(spec: str) -> tuple[str, int, int]:
    """'j=6' or 'k=10:14' (1-based, inclusive) -> (axis, start, count)."""
    m = re.fullmatch(r"\s*([ijkIJK])\s*=\s*(\d+)(?::(\d+))?\s*", spec)
    if not m:
        raise ValueError(f"bad slice '{spec}', expected e.g. j=6 or k=10:14")
    start, end = int(m.group(2)), int(m.group(3) or m.group(2))
    return m.group(1).lower(), start, end - start + 1


def look_at(forward, dist: float) -> list[float]:
    # ResInsight stores the world-to-eye view matrix, row-major, model centred at the origin
    f = [c / math.sqrt(sum(x * x for x in forward)) for c in forward]
    up = (0.0, 0.0, 1.0) if abs(f[2]) < 0.99 else (0.0, 1.0, 0.0)
    r = (f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0])
    n = math.sqrt(sum(x * x for x in r))
    r = [x / n for x in r]
    u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    return [*r, 0.0, *u, 0.0, -f[0], -f[1], -f[2], -dist, 0.0, 0.0, 0.0, 1.0]


def add_slice(proj, case, slice_spec: str, tmp: Path):
    """Range filters cannot be created over gRPC, so write one into a saved project and reopen it."""
    axis, start, count = parse_slice(slice_spec)
    dims = case.grids()[0].dimensions()
    rng = {"i": (1, dims.i), "j": (1, dims.j), "k": (1, dims.k)}
    rng[axis] = (start, count)
    rsp = tmp / "sliced.rsp"
    proj.save(str(rsp))
    text = rsp.read_text()
    if text.count("<CellFilters/>") != 1:
        raise ValueError("view already has cell filters; slice it in the GUI template instead")
    xml = (
        "<CellFilters><CellRangeFilter><IsChecked>True</IsChecked><Name>slice</Name>"
        "<FilterType>INCLUDE</FilterType><GridIndex>0</GridIndex>"
        + "".join(
            f"<StartIndex{a.upper()}>{rng[a][0]}</StartIndex{a.upper()}>"
            f"<CellCount{a.upper()}>{rng[a][1]}</CellCount{a.upper()}>"
            for a in "ijk"
        )
        + "</CellRangeFilter></CellFilters>"
    )
    rsp.write_text(text.replace("<CellFilters/>", xml))
    proj.open(str(rsp))
    return proj.cases()[0], axis


def result_type(case, prop: str) -> str:
    for t in ("DYNAMIC_NATIVE", "STATIC_NATIVE", "GENERATED", "INPUT_PROPERTY"):
        try:
            if prop in case.available_properties(t):
                return t
        except rips.RipsError:  # raised for result types the case has none of
            continue
    raise ValueError(f"property {prop} not in case; use --list to see what is available")


def move_export(tmp: Path, dest: Path) -> Path:
    # ResInsight names exports itself; take the newest image and give it a predictable name
    files = sorted((f for f in tmp.iterdir() if f.suffix.lower() == ".png"), key=os.path.getmtime)
    if not files:
        raise RuntimeError(f"ResInsight exported nothing into {tmp}")
    shutil.move(files[-1], dest)
    return dest


def render_views(case, props, steps_spec, zscale, camera, label, out: Path, tmp: Path) -> list[Path]:
    written = []
    times = case.time_steps()
    views = case.views()
    for vi, view in enumerate(views):
        # update() writes every field back, including a stale time step, so it must precede set_time_step
        if zscale:
            view.grid_z_scale = zscale
        if camera:
            view.camera_matrix = look_at(CAMERAS[camera], -view.camera_matrix[11] * 1.1)
        if zscale or camera:
            view.update()
        current = view.cell_result()
        for prop in props or [current.result_variable]:
            rtype = result_type(case, prop) if props else current.result_type
            if props:
                view.apply_cell_result(rtype, prop)
            steps = [0] if rtype == "STATIC_NATIVE" else parse_steps(steps_spec, len(times))
            for step in steps:
                view.set_time_step(step)
                d = times[step]
                tag = f"_view{vi}" if len(views) > 1 else ""
                name = f"{case.name}{tag}{label}_{prop}_step{step:03d}_{d.year:04d}-{d.month:02d}-{d.day:02d}.png"
                view.export_snapshot(export_folder=str(tmp))
                written.append(move_export(tmp, out / name))
    return written


def summary_case(proj, grid: Path):
    header = grid.with_suffix(".SMSPEC")
    if not header.exists():
        raise ValueError(f"no summary file {header}")
    for s in proj.summary_cases():
        if Path(s.summary_header_filename).resolve() == header:
            return s
    return proj.import_summary_case(str(header))


def render_summary(proj, grid: Path, vectors, out: Path, tmp: Path) -> list[Path]:
    sc = summary_case(proj, grid)
    available = set(sc.available_addresses().values)
    coll = proj.descendants(rips.SummaryPlotCollection)[0]
    classes = rips.generated.generated_classes
    multi = getattr(classes, "MultiSummaryPlot", None)  # absent from older rips releases
    written = []
    for vec in vectors:
        if vec not in available:
            print(f"skip {vec}: not in {grid.stem}.SMSPEC", file=sys.stderr)
            continue
        kind = multi or rips.SummaryPlot
        before = len(proj.descendants(kind))
        coll.new_summary_plot(summary_cases=[sc], address=vec)
        page = proj.descendants(kind)[before]
        if multi:
            # the default 2x2 page squeezes a single curve into one quarter of the image
            page.number_of_columns = classes.NumberOfColumns._1
            page.rows_per_page = classes.RowsPerPage._1
            page.update()
        page.export_snapshot(export_folder=str(tmp))
        written.append(move_export(tmp, out / f"{grid.stem}_{vec.replace(':', '_')}.png"))
    return written


def describe(case, proj, grid: Path) -> str:
    lines = [f"case {case.name}  ({grid})"]
    dims = case.grids()[0].dimensions()
    lines.append(f"  grid: {dims.i} x {dims.j} x {dims.k}")
    for i, d in enumerate(case.time_steps()):
        lines.append(f"  step {i}: {d.year:04d}-{d.month:02d}-{d.day:02d}")
    for t in ("DYNAMIC_NATIVE", "STATIC_NATIVE"):
        lines.append(f"  {t}: {' '.join(sorted(case.available_properties(t)))}")
    try:
        addrs = sorted(summary_case(proj, grid).available_addresses().values)
        wells = sorted({a.split(':', 1)[1] for a in addrs if a.startswith('W') and ':' in a})
        lines.append(f"  summary: {len(addrs)} vectors; wells: {' '.join(wells)}")
    except ValueError:
        lines.append("  summary: none")
    return "\n".join(lines)


@contextlib.contextmanager
def quiet_child_stderr():
    # ResInsight inherits fd 2 and floods it with harmless macOS TSM messages
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


def render(
    case_path: str,
    out: str = "",
    properties: str = "",
    steps: str = "first,last",
    vectors: str = "",
    slice_spec: str = "",
    camera: str = "",
    zscale: float = 0.0,
    template: str = "",
    size: str = "1600x1000",
    list_only: bool = False,
    attach: bool = False,
    exe: str = "",
):
    """Returns the written PNG paths, or the case description when list_only."""
    grid = find_grid(case_path)
    if camera and camera not in CAMERAS:
        raise ValueError(f"camera must be one of {sorted(CAMERAS)}")
    if attach:
        inst = connect()
    else:
        exe = exe or default_executable()
        if not exe:
            raise RuntimeError("ResInsight not found; set RESINSIGHT_EXECUTABLE to its binary")
        # a private instance keeps the user's interactive session untouched
        with quiet_child_stderr():
            inst = rips.Instance.launch(resinsight_executable=exe, launch_port=private_port())
    try:
        proj = inst.project
        w, h = (int(x) for x in size.lower().split("x"))
        proj._execute_command(setMainWindowSize=Cmd.SetWindowSizeParams(width=w, height=h))
        proj._execute_command(setPlotWindowSize=Cmd.SetWindowSizeParams(width=w, height=h))

        if template:
            proj.open(str(Path(template).expanduser().resolve()))
            case = proj.cases()[0]
            case.replace(str(grid))
        else:
            case = proj.load_case(str(grid))
            case.create_view()
        if list_only:
            return describe(case, proj, grid)
        if not out:
            raise ValueError("an output directory is required")

        out_dir = Path(out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / ".ri_export_tmp"
        tmp.mkdir(exist_ok=True)
        label = ""
        if slice_spec:
            case, axis = add_slice(proj, case, slice_spec, tmp)
            camera = camera or SLICE_CAMERA[axis]
            label = "_" + slice_spec.replace("=", "").replace(":", "-")
        elif not template:
            camera = camera or "oblique"

        props = [p.strip() for p in properties.split(",") if p.strip()]
        if not props and not template:
            props = ["PRESSURE"]
        written = render_views(case, props, steps, zscale, camera, label, out_dir, tmp)
        vecs = [v.strip() for v in vectors.split(",") if v.strip()]
        if vecs:
            written += render_summary(proj, grid, vecs, out_dir, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        return written
    finally:
        if not attach:
            inst.exit()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case", help="run directory, .DATA, or .EGRID")
    ap.add_argument("-o", "--out", help="output directory for the PNGs")
    ap.add_argument("-p", "--properties", default="", help="comma list, e.g. PRESSURE,TEMP")
    ap.add_argument("-s", "--steps", default="first,last", help="'all', or indices/first/last, e.g. 0,5,last")
    ap.add_argument("-v", "--vectors", default="", help="summary vectors, e.g. WBHP:B-3H,FOPR")
    ap.add_argument("--slice", default="", help="one IJK slice, 1-based: j=6, or k=10:14 for a slab")
    ap.add_argument("--camera", default="", choices=["", *CAMERAS], help="default: follows the slice, else oblique")
    ap.add_argument("-z", "--zscale", type=float, default=0.0, help="vertical exaggeration")
    ap.add_argument("-t", "--template", help=".rsp whose views (filters, camera, colours) are reused")
    ap.add_argument("--size", default="1600x1000", help="window size WxH requested from ResInsight")
    ap.add_argument("--list", action="store_true", help="print grid, steps, properties and wells, then stop")
    ap.add_argument("--attach", action="store_true", help="use the running ResInsight instead of a private one")
    ap.add_argument("--exe", default="", help="ResInsight binary (default: $RESINSIGHT_EXECUTABLE or a standard install)")
    a = ap.parse_args()
    try:
        result = render(
            a.case, a.out or "", a.properties, a.steps, a.vectors, a.slice, a.camera,
            a.zscale, a.template or "", a.size, a.list, a.attach, a.exe,
        )
    except (ValueError, RuntimeError, rips.RipsError) as exc:
        sys.exit(f"error: {exc}")
    print(result if isinstance(result, str) else "\n".join(map(str, result)))


if __name__ == "__main__":
    main()
