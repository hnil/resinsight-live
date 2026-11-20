"""Write a column CSV of summary vectors (Date;WBHP:F-1H[BARSA];...) as an SMSPEC/UNSMRY pair,
so ResInsight's scripted importSummaryCase can load it as an ordinary summary case."""

from __future__ import annotations

import csv
import datetime as dt
import re
import struct
from pathlib import Path
from typing import List, Optional, Tuple

_DATE_FORMATS = (
    "%d-%m-%Y %H:%M:%S.%f",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)
_HEADER = re.compile(r"^\s*([A-Za-z0-9_]+)(?::([^\[]+?))?\s*(?:\[([^\]]*)\])?\s*$")


def _parse_date(s: str) -> dt.datetime:
    s = s.strip()
    for f in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            pass
    raise ValueError(f"unrecognised date '{s}'")


def read_csv(path: Path) -> Tuple[List[dt.datetime], List[Tuple[str, str, str]], List[List[float]]]:
    text = path.read_text()
    first = text.splitlines()[0] if text else ""
    sep = ";" if first.count(";") >= first.count(",") else ","
    rows = [r for r in csv.reader(text.splitlines(), delimiter=sep) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError(f"{path}: no data rows")
    if rows[0][0].strip().upper() not in ("DATE", "TIME", "DATETIME"):
        raise ValueError(f"{path}: first column must be Date, found '{rows[0][0]}'")
    vectors = []
    for h in rows[0][1:]:
        m = _HEADER.match(h)
        if not m:
            raise ValueError(f"{path}: cannot read column header '{h}'")
        kw, wg, unit = m.group(1).upper(), (m.group(2) or "").strip(), (m.group(3) or "").strip()
        if len(kw) > 8 or len(wg) > 8 or len(unit) > 8:
            raise ValueError(f"{path}: '{h}' has a name or unit longer than 8 characters")
        vectors.append((kw, wg, unit))
    dates, values = [], []
    for r in rows[1:]:
        dates.append(_parse_date(r[0]))
        values.append([float(c) if c.strip() else 0.0 for c in r[1 : len(vectors) + 1]])
    return dates, vectors, values


def _record(f, payload: bytes) -> None:
    n = struct.pack(">i", len(payload))
    f.write(n + payload + n)


def _keyword(f, name: str, kind: str, data: list) -> None:
    _record(f, struct.pack(">8si4s", name.ljust(8).encode(), len(data), kind.encode()))
    if kind == "CHAR":
        for i in range(0, len(data), 105):
            _record(f, b"".join(s.ljust(8)[:8].encode() for s in data[i : i + 105]))
    else:
        code = {"INTE": "i", "REAL": "f"}[kind]
        for i in range(0, len(data), 1000):
            chunk = data[i : i + 1000]
            _record(f, struct.pack(f">{len(chunk)}{code}", *chunk))


def write_summary(csv_path: Path, out_dir: Path, start: Optional[dt.datetime] = None) -> Path:
    """start: simulation start; default is midnight of the first date. Returns the .SMSPEC path."""
    dates, vectors, values = read_csv(csv_path)
    start = start or dates[0].replace(hour=0, minute=0, second=0, microsecond=0)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / csv_path.stem
    kws = ["TIME"] + [v[0] for v in vectors]
    wgs = [":+:+:+:+"] + [v[1] or ":+:+:+:+" for v in vectors]
    units = ["DAYS"] + [v[2] for v in vectors]
    n = len(kws)
    with open(base.with_suffix(".SMSPEC"), "wb") as f:
        _keyword(f, "INTEHEAD", "INTE", [1, 100])  # metric units
        _keyword(f, "RESTART", "CHAR", [""] * 9)
        _keyword(f, "DIMENS", "INTE", [n, 1, 1, 1, 0, -1])
        _keyword(f, "KEYWORDS", "CHAR", kws)
        _keyword(f, "WGNAMES", "CHAR", wgs)
        _keyword(f, "NUMS", "INTE", [0] * n)
        _keyword(f, "UNITS", "CHAR", units)
        _keyword(f, "STARTDAT", "INTE", [start.day, start.month, start.year, start.hour, start.minute,
                                         start.second * 1_000_000 + start.microsecond])
    with open(base.with_suffix(".UNSMRY"), "wb") as f:
        for i, (d, row) in enumerate(zip(dates, values)):
            days = (d - start).total_seconds() / 86400.0
            _keyword(f, "SEQHDR", "INTE", [i])
            _keyword(f, "MINISTEP", "INTE", [i])
            _keyword(f, "PARAMS", "REAL", [days] + row)
    return base.with_suffix(".SMSPEC")
