# resinsight-mcp

Lets Claude — Claude Code, Cowork, or any MCP client — look at and drive a running
[ResInsight](https://resinsight.org) through its Python/gRPC API, and render pictures of
reservoir simulation runs without touching the GUI.

- **Follow the user's session.** `ri_watch` reports what changed in the GUI since the last call
  (time step, cell result, camera, picked cells, cases or views opened). `ri_selected_cells` and
  `ri_picked_cell_report` read the cells the user clicked. `ri_snapshot` returns an image of a
  3D view.
- **Query the model.** `ri_status`, `ri_overview`, `ri_view`, `ri_properties`,
  `ri_property_stats`, `ri_cell_values`, `ri_time_steps`, `ri_wells`, `ri_summary_cases`,
  `ri_summary_values`.
- **Drive the view.** `ri_open`, `ri_create_view`, `ri_set_time_step`, `ri_set_cell_result`,
  `ri_summary_plot`, and `ri_execute_command` for anything else in ResInsight's command API.
- **Render pictures of a run.** `ri_render` starts a private ResInsight, writes PNGs (properties
  at chosen steps, IJK slices, camera presets, summary plots), closes it and returns the images
  inline. The same renderer is a command-line tool, `render.py`.

New users: start with [GETTING_STARTED.md](GETTING_STARTED.md).

The plugin also ships the `resinsight-pictures` skill, which tells Claude how to choose slices,
steps and cameras, and to read the images before describing them.

## Requirements

- **ResInsight with its Python interface.** Official releases
  (https://github.com/OPM/ResInsight/releases) should include it, but so far this plugin has only
  been tested with a source build on macOS; see
  [docs/building-resinsight-macos.md](docs/building-resinsight-macos.md).
- **Python ≥ 3.11**, macOS or Linux. The launcher `bin/run-server` creates its own environment
  in `~/.cache/resinsight-mcp` on first start (about half a minute); later starts are instant.
- **Matching `rips` version.** `rips` must have the same year.month version as ResInsight. The
  launcher installs the newest one; for an older ResInsight set `RIPS_VERSION`, e.g.
  `RIPS_VERSION=2026.6.1.1`.

## Install

**Claude Code**

    claude plugin marketplace add https://github.com/hnil/resinsight-mcp
    claude plugin install resinsight@resinsight-tools

then restart Claude Code.

**Any other MCP client** (stdio):

```json
{ "mcpServers": { "resinsight": { "command": "/path/to/resinsight-mcp/bin/run-server" } } }
```

or one shared HTTP server for several clients:
`bin/run-server --http --port 8765`, then use `http://127.0.0.1:8765/mcp`. The server has no
authentication and controls a desktop application, so it refuses any other `--host` unless you
add `--allow-remote`.

**Claude Desktop.** Quit the app completely (it rewrites its config file while running), then
run `python3 install_desktop.py`.

**Cowork.** The server has to run on the machine where ResInsight runs. Cowork may not be able to
read files the server writes, which is why `ri_render` returns its images inline. Not yet tested
end to end.

## Configuration

| Variable | Purpose |
|---|---|
| `RESINSIGHT_EXECUTABLE` | ResInsight binary used by `ri_render` / `render.py`. Default: `/Applications/ResInsight.app`, `~/Applications/ResInsight.app`, or `ResInsight` on `PATH`. |
| `RIPS_VERSION` | Pin `rips` to your ResInsight version. |
| `RESINSIGHT_GRPC_PORT` | Port of the running ResInsight (default: search 50051–50071). |
| `RESINSIGHT_MCP_PYTHON` | Python used to create the environment (default `python3`). |
| `RESINSIGHT_MCP_VENV` | Where that environment lives. |
| `RESINSIGHT_MCP_ALLOWED_DIRS` | Directories (separated by `:`) the server may read or write; see below. Unset: no limit. |

Put these in `~/.config/resinsight-mcp/env`, one `NAME=value` per line. `bin/run-server` reads
that file, so the settings apply to every client — including apps started from the macOS Dock,
which never see variables from your shell profile:

    RESINSIGHT_EXECUTABLE=/Applications/ResInsight.app/Contents/MacOS/ResInsight
    RESINSIGHT_MCP_ALLOWED_DIRS=$HOME/simulations:$HOME/projects

## Limiting which files the server touches

Claude Code's sandbox confines only its Bash tool. MCP servers, and the ResInsight that
`ri_render` starts, run outside it, and permission rules such as `Read(...)` do not apply to
MCP tools either. With `RESINSIGHT_MCP_ALLOWED_DIRS` set, the server enforces the limit itself:

- every path given to `ri_open`, `ri_render` (case, `out`, `template`) and every file or folder
  argument of `ri_execute_command` must be absolute and resolve, after following symlinks,
  inside one of the directories;
- exports that write to ResInsight's global export folder are refused until `setExportFolder`
  has been called with an allowed directory, and `saveProject` needs an explicit `filePath`;
- commands with file arguments this server does not know about are refused.

`runOctaveScript` is always disabled, since it runs arbitrary code.

What the limit does not cover: files ResInsight opens by itself — the `UNRST`/`SMSPEC` next to a
case (same directory), and the case files a project (`.rsp`) refers to — and anything done in the
ResInsight GUI. `ri_render` and `ri_snapshot` write their own images to the system temp directory
when no output folder is given.

## Starting ResInsight on macOS

Start the binary, not the app bundle:

    /Applications/ResInsight.app/Contents/MacOS/ResInsight &

An app launched with `open` or from Finder gets no permission for `~/Documents` unless you grant
it in System Settings → Privacy & Security, and then cannot load cases stored there.

## render.py

    ~/.cache/resinsight-mcp/venv/bin/python render.py RUN_DIR --list
    ~/.cache/resinsight-mcp/venv/bin/python render.py RUN_DIR -o OUT \
        -p TEMP,STRESSXX -s first,last --slice j=6 -z 5 -v WBHP:B-3H

## Behaviour worth knowing

- The server keeps no state of its own except `ri_watch`'s baseline, so each client sees
  changes since *its own* last call.
- Default ids of `-1` mean "the first case/view"; id `0` is a real id.

## Implementation notes

Things that were not obvious when building this:

- Cell filters cannot be created over gRPC: the `DataFilterCollection` API makes filters that
  no view uses. `render.py` writes the slice into a saved project file
  (`<CellFilters><CellRangeFilter>…`) and reopens it.
- `CameraMatrix` is the world-to-eye view matrix, row-major, with the model centred at the
  origin.
- `PdmObject.update()` writes back *every* field, including a stale `current_time_step`: set
  camera and z-scale before `set_time_step`, never after.
- Summary plots land on a 2×2 page by default; set the `MultiSummaryPlot` to 1×1 before
  exporting, and export the page rather than the `SummaryPlot`.
- rips sends its working directory to ResInsight on connect, and the connection fails if
  ResInsight may not read that folder; the launcher therefore starts the server from `$HOME`.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
