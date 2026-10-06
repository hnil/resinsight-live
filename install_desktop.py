# Copyright 2026 SINTEF.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add the resinsight-live server to Claude Desktop's config. Run with the app fully quit:
the app rewrites the file while running and would drop the entry."""

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
cfg = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
)

data = json.loads(cfg.read_text()) if cfg.exists() else {}
if cfg.exists():
    shutil.copy(cfg, cfg.with_suffix(".json.bak"))
entry = {"command": str(HERE / "bin/run-server")}
# apps started from the Dock do not see shell variables, so carry these over explicitly
keys = ("RESINSIGHT_EXECUTABLE", "RIPS_VERSION", "RESINSIGHT_LIVE_ALLOWED_DIRS", "RESINSIGHT_MCP_ALLOWED_DIRS")
env = {k: os.environ[k] for k in keys if k in os.environ}
if env:
    entry["env"] = env
servers = data.setdefault("mcpServers", {})
old = servers.get("resinsight", {}).get("command", "")
# the old key may belong to another project's server; drop it only if it is this checkout
if old and Path(old).expanduser().resolve().is_relative_to(HERE):
    del servers["resinsight"]
servers["resinsight-live"] = entry
cfg.write_text(json.dumps(data, indent=2) + "\n")
print(f"wrote {cfg}\nservers: {list(servers)}")
