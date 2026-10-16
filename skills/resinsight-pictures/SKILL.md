---
name: resinsight-pictures
description: Make pictures of reservoir/geomechanics simulation results with ResInsight — 3D property snapshots (PRESSURE, TEMP, SWAT, STRESS*, PERMX…) at chosen time steps, IJK slices through the grid, geomechanical results from OPM Flow's VTK output (.pvd: effective/total stress SE/ST, principal stresses, displacement U, pore pressure POR), and summary-vector plots (WBHP, WWIR, FOPR…). Use when asked to show, plot, picture, visualise or snapshot a simulation run, an EGRID/UNRST/SMSPEC case, or a property inside the grid.
---

# ResInsight pictures

Use the `ri_render` tool of the `resinsight-live` MCP server. It launches a **private** ResInsight,
renders PNGs, closes it, and returns the images inline plus the folder they were written to.
The user's own ResInsight session is not touched. (To look at what the user has open in their
own ResInsight instead, use `ri_snapshot` / `ri_view`.)

## Workflow

1. **`ri_render(case=RUN, list_only=True)` first.** It prints grid dimensions, time steps with
   dates, property names and wells. Never guess property names, wells or step indices.
   `case` may be a run directory, `.DATA` or `.EGRID` — always an absolute path.
2. **Anything injection- or well-driven sits inside the grid.** Without `slice_spec` you only
   see the outer faces, which usually show the initial state. Slice through the well: take its
   column (1-based i,j) from `WELSPECS`/`COMPDAT` in the deck and use `slice_spec="j=<j>"`
   (side view) or `"i=<i>"`; `"k=<k>"` gives a map view of one layer, `"k=10:14"` a slab.
   Pick k where the effect is, not the top layer.
3. Thin reservoirs need `zscale=5` (or more) to be readable in side views.
4. `steps="first,last"` shows the change; `"all"` the evolution; or explicit indices.
5. `camera` = `top|front|side|oblique` overrides the default, which follows the slice axis
   (oblique when there is no slice).
6. `vectors="WBHP:<well>,WWIR:<well>"` adds summary plots, one vector per image.
7. For a view set up by hand (several filters, colours, legends) the user can save an `.rsp`
   in ResInsight and pass it as `template`; its views are reused with the run swapped in.

Put all properties and steps for one case in one call — each call pays a few seconds to start
ResInsight. At most `max_images` come back inline; the listing names every file.

## Geomechanics (OPM Flow VTK output)

For stress, strain and displacement from `flow_energy_geomech`, render the geomechanical case.
The run must have `--enable-vtk-output=true --vtk-write-geo-mech-nodal=true` (pore pressure and
IJK come from that flag), and ResInsight must be a 2026.9+ build with the geomechanics scripting.

1. **`ri_render(case=RUN, geomech=True, list_only=True)`** (or `case=.../CASE.pvd`). It lists the
   steps by name — `Dataset 432000` is the simulator time in seconds (5 days) — the IJK size and
   the results per position. Never guess result names.
2. **properties** are `FIELD:COMPONENT`: `SE:S1` (largest effective principal stress, compression
   positive, bar), `ST:S33` (total vertical stress, bar), `POR-Bar`, `U:U3` (vertical
   displacement, m), `NE:E1`, `SFI` … The position is found automatically; force it with a prefix,
   e.g. `NODAL/POR-Bar` or `ELEMENT/S_VTK:S33` (the simulator's raw total stress, Pa).
3. **Slices and cameras work as for grids** (`slice_spec="i=6"`), with the flow grid's IJK, also
   across faults. Use `zscale` the same way.
4. **What the colours are.** Stress and strain are per cell: every corner of an element shows its
   cell's value, so the picture is blocky by design. POR is recovered at the nodes from the cells
   and smooths the injection peak a little; U is the computed node displacement.
5. **The depth gradient hides the injection.** POR, ST and SE are dominated by depth; a plume shows
   better in `U:U3`, in `SE` at `steps="first,last"` side by side, or in the flow case's PRESSURE.

## Always look at the result

Check the legend range and the statistics box in each image (min/max/P10/P90 of the *visible*
cells). A slice that is one uniform colour with min≈max missed the plume: move it rather than
present it. Describe what a picture shows with the numbers from that box, not just "see image".

## Limits

- Summary plots show one case per image; for overlaying many runs use a dedicated plotting tool.
- 3D images come out at roughly 800×750 regardless of the requested size.
- Slices are one IJK range per call.
- Geomechanical cases cannot use `template`.
- On ResInsight 2026.9 development builds summary plots export nothing (an upstream change to the
  plot windows); 3D pictures are not affected.
