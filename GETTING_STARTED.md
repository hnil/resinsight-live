# ResInsight for Claude — getting started

This plugin lets Claude work with [ResInsight](https://resinsight.org) in two ways:

- **It follows your ResInsight session.** Claude can see which case, property and time step
  you are looking at, read the values in the cells you click, notice what you changed, take a
  picture of your 3D view, and open cases or plots for you.
- **It makes pictures of simulation runs on its own.** Claude starts a hidden ResInsight,
  renders the pictures you ask for — a property at chosen time steps, a slice through the grid,
  a summary plot — and closes it again. Your own ResInsight is not touched.

Things you can ask once it is installed:

- *"Show TEMP through the injector column of /path/to/run at the first and last step."*
- *"What are the properties of the cell I just clicked?"*
- *"Plot WBHP and WWIR for B-3H."*
- *"What did I change in ResInsight since you last looked?"*

> **Repository:** https://github.com/hnil/resinsight-live

---

## 1. Install ResInsight

Download the release for your system from https://github.com/OPM/ResInsight/releases. On
macOS, unzip it into `/Applications`; if macOS refuses to open it, look for macOS notes included
with the download.

> So far the plugin has been tested with a ResInsight built from source on macOS, not with the
> official releases. If Claude cannot reach an official release, please report it.

## 2. Check Python

You need Python 3.11 or newer:

```bash
python3 --version
```

On macOS, `brew install python` if it is older or missing. The plugin creates its own Python
environment the first time it starts, so you do not install any packages yourself.

## 3. Install the plugin in Claude Code

```bash
claude plugin marketplace add https://github.com/hnil/resinsight-live
claude plugin install resinsight-live@hnil-reservoir-tools
```

Restart Claude Code. `claude mcp list` should now show `plugin:resinsight-live:resinsight-live … ✓ Connected`.
The very first start takes up to half a minute while the Python environment is built.

## 4. Create the settings file

Create `~/.config/resinsight-live/env` with one `NAME=value` per line:

```
RESINSIGHT_LIVE_ALLOWED_DIRS=$HOME/Documents/simulations
RESINSIGHT_EXECUTABLE=/Applications/ResInsight.app/Contents/MacOS/ResInsight
```

| Setting | What to put there |
|---|---|
| `RESINSIGHT_LIVE_ALLOWED_DIRS` | The folders with your simulation runs, separated by `:`. Claude can only open files and write pictures inside these. **Recommended** — see *Security*. |
| `RESINSIGHT_EXECUTABLE` | Only needed if ResInsight is not in `/Applications` (or `ResInsight` on your `PATH` on Linux). |
| `RIPS_VERSION` | Only needed if your ResInsight is not the newest release. It must have the same year and month as your ResInsight, e.g. `2026.6.1.1` for ResInsight 2026.06; versions are listed at https://pypi.org/project/rips/#history. |
| `RESINSIGHT_GRPC_PORT` | Only if you changed ResInsight's scripting port from 50051. |

The file is read every time the server starts; restart Claude Code after changing it.

## 5. Start ResInsight

On macOS, start the program itself rather than double-clicking it:

```bash
/Applications/ResInsight.app/Contents/MacOS/ResInsight &
```

Started from Finder or with `open`, macOS does not let ResInsight read your `Documents` folder
unless you allow it under System Settings → Privacy & Security → Files and Folders, and cases
stored there then fail to load.

## 6. Try it

Ask Claude: *"Check that you can reach ResInsight."* It should answer with the ResInsight
version and the cases you have open. Then: *"List the time steps, properties and wells of
/path/to/run."*

---

## Claude Desktop and Cowork

1. `git clone https://github.com/hnil/resinsight-live` somewhere permanent.
2. Quit Claude completely with Cmd-Q — closing the window is not enough. The app rewrites its
   configuration file while it runs and would undo the change.
3. Run `python3 <clone>/install_desktop.py`, then start Claude again.

The server runs on your own computer, next to ResInsight. Cowork may not be able to open files on
your computer, so pictures are handed to it directly. This setup has not been tested end to end
yet.

## Security

Claude Code's sandbox only restricts the commands Claude runs in its terminal. It does **not**
restrict MCP servers such as this one, and file permission rules in Claude's settings do not
apply to them either. The plugin therefore enforces its own limit through
`RESINSIGHT_LIVE_ALLOWED_DIRS`:

- Every file or folder Claude asks ResInsight to open, load, import or export to must lie inside
  those folders (symbolic links are followed, so they cannot lead outside).
- Exports that would go to ResInsight's default export folder are refused until an allowed
  export folder has been set, and saving a project needs an explicit location.
- Running Octave scripts through ResInsight is always disabled.

Not covered: files ResInsight opens by itself (the result files next to a case, and the cases a
saved project refers to), and anything you do yourself in the ResInsight window.

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| "Could not find any ResInsight instances" | ResInsight is not running, or listens on another port | Start it (step 5); set `RESINSIGHT_GRPC_PORT` if you changed the port. |
| A case in `Documents` fails to load, or "Path does not exist" | ResInsight was started from Finder or with `open` | Start the program as in step 5, or grant Documents access. |
| An error about incompatible versions | `rips` and ResInsight differ in year/month | Set `RIPS_VERSION` (step 4) and restart Claude Code. |
| "… is outside the allowed directories" | The file is not in `RESINSIGHT_LIVE_ALLOWED_DIRS` | Add its folder to the setting, or move the run. |
| "ResInsight not found; set RESINSIGHT_EXECUTABLE" | Picture rendering cannot find the program | Set `RESINSIGHT_EXECUTABLE` (step 4). |
| The tools do not show up | Claude Code was not restarted, or the server failed to start | Restart; check `claude mcp list`. |
| Settings you added in Claude Desktop disappear | The file was edited while the app was running | Quit with Cmd-Q first (see *Claude Desktop and Cowork*). |

## Updating and removing

```bash
claude plugin marketplace update hnil-reservoir-tools
claude plugin update resinsight-live@hnil-reservoir-tools
```

To remove it: `claude plugin uninstall resinsight-live@hnil-reservoir-tools`.

Coming from version 0.1 (`resinsight@resinsight-tools`)? Uninstall that, remove its marketplace
(`claude plugin marketplace remove resinsight-tools`), then install as in step 3. Your old
`~/.config/resinsight-mcp/env` is still read for now; rename the folder to `resinsight-live`.

Works on macOS and Linux; Windows is not supported yet.
