#!/usr/bin/env python3
"""drplcmp — Find Drupal 10+ nodes that differ between two SQL backups.

Reads two .sql backup files (mysqldump-style) of a Drupal 10+ database and
reports which nodes of a given content type were created, modified, or
deleted within a supplied [start, end] datetime window.

The script does not execute SQL or invoke a shell. Inputs are validated and
parsed with a self-contained tokenizer to prevent injection.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# ANSI color codes (no third-party dependency).
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

# Tables we recognise as part of a Drupal 10+ dump.  Two or more of these
# must be present for the file to be accepted as a Drupal backup.
DRUPAL_SIGNATURE_TABLES = (
    "node",
    "node_field_data",
    "node_field_revision",
    "node_revision",
    "users_field_data",
    "key_value",
    "config",
    "cache_default",
    "sequences",
    "semaphore",
)
MIN_DRUPAL_SIGNATURES = 2

# Drupal content type machine names: lowercase letters, digits, underscores.
MACHINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Recognise the mysqldump completion marker.
DUMP_DATE_RE = re.compile(
    r"^--\s*Dump completed(?: on)?\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})"
)

# Canonical table suffix we extract row data for.  Real-world Drupal sites
# may install with a table prefix (e.g. ``drup_node_field_data``), so we
# accept any table whose unprefixed name is ``node_field_data``.
NODE_FIELD_DATA = "node_field_data"
TARGET_TABLES = (NODE_FIELD_DATA,)


def _is_node_field_data(table: str) -> bool:
    """True if *table* is ``node_field_data`` with or without a prefix."""
    return table == NODE_FIELD_DATA or table.endswith("_" + NODE_FIELD_DATA)


def _matches_signature(table: str, signature: str) -> bool:
    """True if *table* equals or is a prefixed form of *signature*."""
    return table == signature or table.endswith("_" + signature)


# ---------------------------------------------------------------------------
# Argparse helpers / type validators
# ---------------------------------------------------------------------------
def validate_sql_file(value: str) -> Path:
    """argparse type — accept only existing, non-empty files ending in .sql."""
    p = Path(value)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"file not found: {value!r}")
    if not p.is_file():
        raise argparse.ArgumentTypeError(f"not a regular file: {value!r}")
    if p.suffix.lower() != ".sql":
        raise argparse.ArgumentTypeError(
            f"file does not have .sql extension: {value!r}"
        )
    if p.stat().st_size == 0:
        raise argparse.ArgumentTypeError(f"file is empty: {value!r}")
    return p


def validate_machine_name(value: str) -> str:
    """argparse type — accept only Drupal-style machine names."""
    if not MACHINE_NAME_RE.match(value):
        raise argparse.ArgumentTypeError(
            "content type machine name must match [a-z][a-z0-9_]{0,31}: "
            f"got {value!r}"
        )
    return value


def parse_datetime_arg(value: str) -> datetime:
    """argparse type — accept several common ISO-ish formats (UTC)."""
    formats = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc)
    raise argparse.ArgumentTypeError(
        f"could not parse datetime {value!r}; use e.g. 2025-01-15T12:00:00"
    )


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


# ---------------------------------------------------------------------------
# SQL tokenizer / value parser
# ---------------------------------------------------------------------------
def iter_sql_statements(text: str) -> Iterator[str]:
    """Yield top-level SQL statements from *text*.

    Tracks string literals, line comments (-- and #), and block comments
    (/* ... */) so that semicolons inside those constructs are not treated
    as statement terminators.
    """
    n = len(text)
    i = 0
    start = 0
    in_string = False
    in_line_comment = False
    in_block_comment = False
    line_comment_start_char = ""
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "'":
                in_string = False
            i += 1
            continue
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        # Not in any special construct.
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            in_line_comment = True
            line_comment_start_char = "-"
            i += 2
            continue
        if c == "#":
            in_line_comment = True
            line_comment_start_char = "#"
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if c == "'":
            in_string = True
            i += 1
            continue
        if c == ";":
            stmt = text[start : i + 1].strip()
            if stmt:
                yield stmt
            start = i + 1
            i += 1
            continue
        i += 1
    tail = text[start:].strip()
    if tail:
        yield tail


_SQL_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\x00",
    "b": "\b",
    "Z": "\x1a",
    "'": "'",
    '"': '"',
    "\\": "\\",
}


def parse_value_tuples(text: str) -> list[tuple]:
    """Parse a string of the form ``(...),(...),...;`` into list of tuples.

    Handles quoted strings with mysqldump-style backslash escapes and the
    SQL-standard doubled-quote ('') escape, integer / float / NULL literals.
    """
    tuples: list[tuple] = []
    i = 0
    n = len(text)
    while i < n:
        # Skip whitespace and inter-tuple commas.
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == ";":
            break
        if text[i] != "(":
            break
        i += 1
        values: list = []
        while True:
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i >= n:
                break
            ch = text[i]
            if ch == "'":
                # Quoted string literal.
                i += 1
                buf: list[str] = []
                while i < n:
                    cc = text[i]
                    if cc == "\\" and i + 1 < n:
                        nxt = text[i + 1]
                        buf.append(_SQL_ESCAPES.get(nxt, nxt))
                        i += 2
                        continue
                    if cc == "'":
                        # Doubled '' is a literal apostrophe.
                        if i + 1 < n and text[i + 1] == "'":
                            buf.append("'")
                            i += 2
                            continue
                        i += 1
                        break
                    buf.append(cc)
                    i += 1
                values.append("".join(buf))
            else:
                # Unquoted token: NULL, integer, float, or bareword.
                start = i
                depth = 0
                while i < n:
                    cc = text[i]
                    if cc == "(":
                        depth += 1
                    elif cc == ")" and depth > 0:
                        depth -= 1
                    elif cc in ",)" and depth == 0:
                        break
                    i += 1
                tok = text[start:i].strip()
                if tok.upper() == "NULL":
                    values.append(None)
                else:
                    try:
                        if "." in tok or "e" in tok.lower():
                            values.append(float(tok))
                        else:
                            values.append(int(tok))
                    except ValueError:
                        values.append(tok)
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == ")":
                i += 1
                break
            break
        tuples.append(tuple(values))
    return tuples


_CREATE_TABLE_RE = re.compile(
    r"^CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`([^`]+)`\s*\((.*)\)[^)]*$",
    re.IGNORECASE | re.DOTALL,
)
_NON_COLUMN_KEYWORDS = {
    "KEY",
    "PRIMARY",
    "UNIQUE",
    "CONSTRAINT",
    "INDEX",
    "FULLTEXT",
    "SPATIAL",
    "FOREIGN",
    "CHECK",
}


def parse_create_table(stmt: str) -> Optional[tuple[str, list[str]]]:
    """Return (table_name, [column names]) for a CREATE TABLE statement."""
    m = _CREATE_TABLE_RE.match(stmt.strip().rstrip(";").strip())
    if not m:
        return None
    table = m.group(1)
    body = m.group(2)
    cols: list[str] = []
    for line in body.split("\n"):
        s = line.strip().rstrip(",")
        if not s.startswith("`"):
            continue
        m2 = re.match(r"`([^`]+)`\s+([A-Za-z]+)", s)
        if not m2:
            continue
        if m2.group(2).upper() in _NON_COLUMN_KEYWORDS:
            continue
        cols.append(m2.group(1))
    return table, cols


_INSERT_RE = re.compile(
    r"^INSERT\s+(?:IGNORE\s+)?INTO\s+`([^`]+)`\s*"
    r"(?:\(\s*((?:`[^`]+`\s*,?\s*)+)\)\s*)?"
    r"VALUES\s*",
    re.IGNORECASE,
)


def parse_insert(
    stmt: str, columns_map: dict[str, list[str]]
) -> Optional[tuple[str, list[dict]]]:
    """Parse an INSERT INTO statement into (table_name, list_of_row_dicts)."""
    m = _INSERT_RE.match(stmt.lstrip())
    if not m:
        return None
    table = m.group(1)
    explicit = m.group(2)
    if explicit:
        cols = [c.strip().strip("`") for c in explicit.split(",") if c.strip()]
    else:
        cols = columns_map.get(table, [])
    body = stmt.lstrip()[m.end() :]
    if body.endswith(";"):
        body = body[:-1]
    tuples = parse_value_tuples(body)
    rows: list[dict] = []
    for t in tuples:
        if cols and len(cols) == len(t):
            rows.append(dict(zip(cols, t)))
        else:
            rows.append({i: v for i, v in enumerate(t)})
    return table, rows


# ---------------------------------------------------------------------------
# Dump-level operations
# ---------------------------------------------------------------------------
class DumpData:
    """Accumulated facts about a single .sql backup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.columns_map: dict[str, list[str]] = {}
        self.seen_tables: set[str] = set()
        self.rows: dict[str, list[dict]] = {t: [] for t in TARGET_TABLES}
        self.dump_completed_at: Optional[datetime] = None
        self.sha256: Optional[str] = None
        self.mtime: float = 0.0


def sha256_of_file(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def parse_dump(path: Path) -> DumpData:
    """Read *path* and return a DumpData with extracted rows and metadata."""
    data = DumpData(path)
    data.sha256 = sha256_of_file(path)
    data.mtime = path.stat().st_mtime
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Find dump-completed marker before stripping comments.
    for line in raw.splitlines():
        m = DUMP_DATE_RE.match(line.strip())
        if m:
            try:
                data.dump_completed_at = datetime.strptime(
                    f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                data.dump_completed_at = None
            break
    for stmt in iter_sql_statements(raw):
        head = stmt.lstrip()[:32].upper()
        if head.startswith("CREATE TABLE"):
            r = parse_create_table(stmt)
            if r:
                table, cols = r
                data.columns_map[table] = cols
                data.seen_tables.add(table)
        elif head.startswith("INSERT INTO") or head.startswith("INSERT IGNORE"):
            r = parse_insert(stmt, data.columns_map)
            if r:
                table, rows = r
                data.seen_tables.add(table)
                if _is_node_field_data(table):
                    data.rows[NODE_FIELD_DATA].extend(rows)
    return data


def looks_like_drupal(data: DumpData) -> bool:
    matches = 0
    for sig in DRUPAL_SIGNATURE_TABLES:
        if any(_matches_signature(t, sig) for t in data.seen_tables):
            matches += 1
    return matches >= MIN_DRUPAL_SIGNATURES


def dump_contains_content_type(data: DumpData, machine_name: str) -> bool:
    for row in data.rows.get("node_field_data", []):
        if row.get("type") == machine_name:
            return True
    return False


# ---------------------------------------------------------------------------
# Difference detection
# ---------------------------------------------------------------------------
def _to_unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _row_changed(row: dict) -> Optional[int]:
    v = row.get("changed")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def find_differing_nids(
    data_a: DumpData,
    data_b: DumpData,
    content_type: str,
    start: datetime,
    end: datetime,
) -> tuple[list[int], dict[int, dict], dict[int, dict]]:
    """Return (sorted differing nids, nid->row_a map, nid->row_b map).

    A node qualifies when:
      * it is of *content_type* in at least one backup, AND
      * its `changed` timestamp in at least one backup falls in [start, end], AND
      * the row data differs between the two backups (added, removed, or modified).
    """
    start_ts = _to_unix(start)
    end_ts = _to_unix(end)

    def index(rows: list[dict]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for r in rows:
            if r.get("type") != content_type:
                continue
            nid = r.get("nid")
            try:
                nid_int = int(nid)
            except (TypeError, ValueError):
                continue
            out[nid_int] = r
        return out

    map_a = index(data_a.rows.get("node_field_data", []))
    map_b = index(data_b.rows.get("node_field_data", []))

    candidates: set[int] = set()
    for nid, r in map_a.items():
        ch = _row_changed(r)
        if ch is not None and start_ts <= ch <= end_ts:
            candidates.add(nid)
    for nid, r in map_b.items():
        ch = _row_changed(r)
        if ch is not None and start_ts <= ch <= end_ts:
            candidates.add(nid)

    differing: list[int] = []
    for nid in candidates:
        ra = map_a.get(nid)
        rb = map_b.get(nid)
        if ra != rb:
            differing.append(nid)
    differing.sort()
    return differing, map_a, map_b


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------
def _serialize_row(row: Optional[dict]) -> list[str]:
    if row is None:
        return ["<absent in this backup>"]
    keys = sorted(row.keys(), key=str)
    return [f"{k}: {row[k]!r}" for k in keys]


def render_diff(
    nid: int,
    label_a: str,
    label_b: str,
    row_a: Optional[dict],
    row_b: Optional[dict],
    use_color: bool,
) -> str:
    a_lines = _serialize_row(row_a)
    b_lines = _serialize_row(row_b)
    diff = list(
        difflib.unified_diff(
            a_lines, b_lines, fromfile=label_a, tofile=label_b, lineterm=""
        )
    )

    def colorize(line: str) -> str:
        if not use_color:
            return line
        if line.startswith("+++") or line.startswith("---"):
            return f"{BOLD}{line}{RESET}"
        if line.startswith("+"):
            return f"{GREEN}{line}{RESET}"
        if line.startswith("-"):
            return f"{RED}{line}{RESET}"
        if line.startswith("@@"):
            return f"{CYAN}{line}{RESET}"
        return line

    header = f"=== node {nid} ==="
    if use_color:
        header = f"{BOLD}{MAGENTA}{header}{RESET}"
    body = "\n".join(colorize(line) for line in diff)
    if not body:
        body = "(no textual difference)"
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Output format renderers
# ---------------------------------------------------------------------------
OUTPUT_FORMATS = ("plain", "table", "json", "proof")


def _classify(nid: int, map_a: dict, map_b: dict) -> str:
    a = map_a.get(nid)
    b = map_b.get(nid)
    if a is None and b is not None:
        return "added"
    if b is None and a is not None:
        return "removed"
    return "modified"


def _fmt_unix_ts(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError):
        return str(value)


def _truncate(s: Any, n: int) -> str:
    s = "—" if s is None else str(s)
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


def render_plain(
    nids: list[int],
    map_a: dict,
    map_b: dict,
    args: argparse.Namespace,
    data_a: DumpData,
    data_b: DumpData,
) -> str:
    if not nids:
        return "no differing node ids found in window"
    lines = ["Differing node ids:"]
    for nid in nids:
        lines.append(f"  {nid}")
    return "\n".join(lines)


def _format_box_table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def hline(left: str, mid: str, right: str) -> str:
        parts = ["─" * (w + 2) for w in widths]
        return left + mid.join(parts) + right

    def row_line(cells: list[str]) -> str:
        return (
            "│"
            + "│".join(
                f" {cells[i].ljust(widths[i])} " for i in range(cols)
            )
            + "│"
        )

    out = [
        hline("┌", "┬", "┐"),
        row_line(headers),
        hline("├", "┼", "┤"),
    ]
    for row in rows:
        out.append(row_line(row))
    out.append(hline("└", "┴", "┘"))
    return "\n".join(out)


def render_table(
    nids: list[int],
    map_a: dict,
    map_b: dict,
    args: argparse.Namespace,
    data_a: DumpData,
    data_b: DumpData,
) -> str:
    if not nids:
        return "no differing node ids found in window"
    headers = ["NID", "Status", "Title (A)", "Title (B)", "Changed (A)", "Changed (B)"]
    rows: list[list[str]] = []
    for nid in nids:
        a = map_a.get(nid) or {}
        b = map_b.get(nid) or {}
        rows.append(
            [
                str(nid),
                _classify(nid, map_a, map_b),
                _truncate(a.get("title"), 36),
                _truncate(b.get("title"), 36),
                _fmt_unix_ts(a.get("changed")),
                _fmt_unix_ts(b.get("changed")),
            ]
        )
    summary = (
        f"{len(nids)} differing node id(s) "
        f"for content type {args.content_type!r} "
        f"in [{args.start.date()}, {args.end.date()}]"
    )
    return summary + "\n" + _format_box_table(headers, rows)


def render_json(
    nids: list[int],
    map_a: dict,
    map_b: dict,
    args: argparse.Namespace,
    data_a: DumpData,
    data_b: DumpData,
) -> str:
    payload = {
        "tool": "drplcmp",
        "version": __version__,
        "parameters": {
            "content_type": args.content_type,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "limit": args.limit,
        },
        "count": len(nids),
        "node_ids": list(nids),
        "items": [
            {
                "nid": nid,
                "status": _classify(nid, map_a, map_b),
                "row_a": map_a.get(nid),
                "row_b": map_b.get(nid),
            }
            for nid in nids
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding suitable for hashing."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )


def build_proof_manifest(
    nids: list[int],
    map_a: dict,
    map_b: dict,
    args: argparse.Namespace,
    data_a: DumpData,
    data_b: DumpData,
) -> dict:
    """Return a tamper-evident manifest of inputs, parameters, and result.

    ``proof_sha256`` is computed over a canonical JSON encoding of the
    proof-relevant subset (input file digests + parameters + result),
    excluding clock-dependent or path-dependent metadata so that anyone
    re-running the tool with identical inputs and parameters can reproduce
    the same proof_sha256 byte-for-byte.
    """
    items = [
        {"nid": nid, "a": map_a.get(nid), "b": map_b.get(nid)} for nid in nids
    ]
    rows_canonical = _canonical_json(items)
    rows_sha256 = hashlib.sha256(rows_canonical.encode("utf-8")).hexdigest()

    proof_input = {
        "inputs": {
            "file_a_sha256": data_a.sha256,
            "file_b_sha256": data_b.sha256,
        },
        "parameters": {
            "content_type": args.content_type,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "limit": args.limit,
        },
        "result": {
            "count": len(nids),
            "node_ids": list(nids),
            "rows_canonical_sha256": rows_sha256,
        },
    }
    canonical = _canonical_json(proof_input)
    proof_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def file_info(d: DumpData) -> dict:
        return {
            "path": str(d.path),
            "sha256": d.sha256,
            "size": d.path.stat().st_size,
            "mtime": d.mtime,
        }

    return {
        "tool": "drplcmp",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"file_a": file_info(data_a), "file_b": file_info(data_b)},
        "parameters": proof_input["parameters"],
        "result": {
            **proof_input["result"],
            "items": items,
        },
        "proof_sha256": proof_sha256,
    }


def render_proof(
    nids: list[int],
    map_a: dict,
    map_b: dict,
    args: argparse.Namespace,
    data_a: DumpData,
    data_b: DumpData,
) -> str:
    return json.dumps(
        build_proof_manifest(nids, map_a, map_b, args, data_a, data_b),
        indent=2,
        sort_keys=True,
        default=str,
    )


_RENDERERS = {
    "plain": render_plain,
    "table": render_table,
    "json": render_json,
    "proof": render_proof,
}


# ---------------------------------------------------------------------------
# Validation orchestration
# ---------------------------------------------------------------------------
class ValidationError(Exception):
    """Raised when the user-supplied inputs fail validation."""


def validate_inputs(
    file_a: Path, file_b: Path, content_type: str
) -> tuple[DumpData, DumpData]:
    """Run all input checks. Return parsed DumpData for the two files.

    Raises ValidationError with a human-readable message on the first failure.
    """
    if file_a.resolve() == file_b.resolve():
        raise ValidationError(
            f"both inputs point to the same file: {file_a.resolve()}"
        )

    data_a = parse_dump(file_a)
    data_b = parse_dump(file_b)

    if not looks_like_drupal(data_a):
        raise ValidationError(
            f"{file_a} does not look like a Drupal backup "
            f"(saw tables: {sorted(data_a.seen_tables)[:6]}…)"
        )
    if not looks_like_drupal(data_b):
        raise ValidationError(
            f"{file_b} does not look like a Drupal backup "
            f"(saw tables: {sorted(data_b.seen_tables)[:6]}…)"
        )

    if data_a.sha256 == data_b.sha256:
        raise ValidationError(
            "input files are byte-for-byte identical (same sha256)"
        )

    signals_differ = False
    reasons: list[str] = []
    if data_a.dump_completed_at and data_b.dump_completed_at:
        if data_a.dump_completed_at != data_b.dump_completed_at:
            signals_differ = True
            reasons.append("mysqldump completion timestamps differ")
    if abs(data_a.mtime - data_b.mtime) > 0.5:
        signals_differ = True
        reasons.append("file mtimes differ")
    # Content already differs (sha256 check above) — that's a valid signal too.
    signals_differ = signals_differ or data_a.sha256 != data_b.sha256
    if not signals_differ:
        raise ValidationError(
            "could not detect any datetime/mtime/content difference between inputs"
        )

    if not (
        dump_contains_content_type(data_a, content_type)
        or dump_contains_content_type(data_b, content_type)
    ):
        raise ValidationError(
            f"neither backup contains any node of content type {content_type!r}"
        )

    return data_a, data_b


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drplcmp",
        description=(
            "Find Drupal 10+ nodes that differ between two SQL backups within "
            "a given content type and time window."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "file_a",
        metavar="BACKUP_A.sql",
        type=validate_sql_file,
        help="earlier SQL backup (mysqldump-style .sql file)",
    )
    parser.add_argument(
        "file_b",
        metavar="BACKUP_B.sql",
        type=validate_sql_file,
        help="later SQL backup (mysqldump-style .sql file)",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="content_type",
        required=True,
        type=validate_machine_name,
        help="content type machine name (e.g. article)",
    )
    parser.add_argument(
        "-s",
        "--start",
        required=True,
        type=parse_datetime_arg,
        help="window start datetime (UTC, e.g. 2025-01-01T00:00:00)",
    )
    parser.add_argument(
        "-e",
        "--end",
        required=True,
        type=parse_datetime_arg,
        help="window end datetime (UTC, inclusive)",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=positive_int,
        default=None,
        help="restrict the returned list of node ids to N entries",
    )
    parser.add_argument(
        "--diff",
        metavar="NID",
        type=positive_int,
        default=None,
        help="show a colorful text diff for one specific node id",
    )
    parser.add_argument(
        "--diff-all",
        action="store_true",
        help="show colorful text diffs for every identified node id",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color in output (auto-disabled when stdout is not a tty)",
    )
    parser.add_argument(
        "-f",
        "--format",
        dest="output_format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help=(
            "result format: plain (list), table (pretty box), json (structured), "
            "proof (tamper-evident JSON manifest with deterministic SHA-256 over "
            "input digests + parameters + result)"
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)

    if args.start > args.end:
        parser.error("--start must be <= --end")

    use_color = not args.no_color and sys.stdout.isatty()

    try:
        data_a, data_b = validate_inputs(
            args.file_a, args.file_b, args.content_type
        )
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    nids, map_a, map_b = find_differing_nids(
        data_a, data_b, args.content_type, args.start, args.end
    )
    limited = nids[: args.limit] if args.limit else nids

    renderer = _RENDERERS[args.output_format]
    print(renderer(limited, map_a, map_b, args, data_a, data_b))

    # Textual unified diffs only make sense for human-readable formats.
    if args.output_format in ("plain", "table"):
        label_a = str(args.file_a)
        label_b = str(args.file_b)
        if args.diff is not None:
            if args.diff not in limited:
                print(
                    f"\nnote: nid {args.diff} is not in the differing set "
                    "(showing diff anyway if data exists)",
                    file=sys.stderr,
                )
            print()
            print(
                render_diff(
                    args.diff,
                    label_a,
                    label_b,
                    map_a.get(args.diff),
                    map_b.get(args.diff),
                    use_color,
                )
            )
        if args.diff_all:
            for nid in limited:
                print()
                print(
                    render_diff(
                        nid,
                        label_a,
                        label_b,
                        map_a.get(nid),
                        map_b.get(nid),
                        use_color,
                    )
                )

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
