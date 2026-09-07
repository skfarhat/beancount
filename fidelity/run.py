#!/usr/bin/env python3
"""Fidelity harness: diff Python beanquery vs this Go fork over a real ledger.

For each BQL query in queries.jsonl, run it through BOTH engines with identical
flags (`-f csv -m`, i.e. numberified CSV) and compare the result tables.

- Python engine: `uv run bean-query` inside the sf-money project (its .venv has
  beancount 3.x + beanquery installed).
- Go engine: the `beancount` CLI built fresh from this fork.

Numeric cells are compared with a small tolerance (formatting/precision differ
between engines); everything else is string-compared. Rows are compared in order
when the query has ORDER BY, otherwise as a multiset.

Usage:
    python3 fidelity/run.py                 # uses defaults below
    SF_MONEY=~/d/code/sf-money python3 fidelity/run.py

Exit code is the number of non-matching queries (0 = full fidelity).
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- config (env-overridable) ------------------------------------------------
HERE = Path(__file__).resolve().parent
FORK = HERE.parent
SF_MONEY = Path(os.path.expanduser(os.environ.get("SF_MONEY", "~/d/code/sf-money")))
BEANFILE = Path(
    os.path.expanduser(
        os.environ.get("BEANFILE", str(SF_MONEY / "beanfiles" / "money.bean"))
    )
).resolve()
QUERIES = Path(os.environ.get("QUERIES", str(HERE / "queries.jsonl")))
REPORT = Path(os.environ.get("REPORT", str(HERE / "report.txt")))
# Tolerances absorb sub-cent CONVERT rounding noise (Go keeps full decimal
# precision; Python rounds per-step), which is immaterial for a finance app.
# The harness is here to catch MATERIAL divergence, not penny dust.
ABS_TOL = float(os.environ.get("ABS_TOL", "0.05"))   # 5 cents
REL_TOL = float(os.environ.get("REL_TOL", "1e-4"))   # 0.01%
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class EngineRun:
    ok: bool          # process exited 0
    rows: list        # parsed CSV rows (list of tuples), header excluded
    header: list      # header cells
    raw: str          # stdout (or stderr on failure)


def build_go() -> str:
    """Build the Go CLI from the fork; return the binary path."""
    out = HERE / ".gobean"
    print(f"{DIM}building go binary from {FORK} ...{RESET}")
    r = subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/..."],
        cwd=FORK, capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"go build failed:\n{r.stderr}")
    return str(out)


def _parse_csv(text: str) -> tuple[list, list]:
    reader = csv.reader(io.StringIO(text))
    all_rows = [tuple(c.strip() for c in row) for row in reader if row]
    if not all_rows:
        return [], []
    return list(all_rows[0]), all_rows[1:]


def run_python(q: str) -> EngineRun:
    r = subprocess.run(
        ["uv", "run", "bean-query", "-f", "csv", "-m", "-q", str(BEANFILE), q],
        cwd=SF_MONEY, capture_output=True, text=True, timeout=TIMEOUT,
    )
    if r.returncode != 0:
        return EngineRun(False, [], [], r.stderr or r.stdout)
    header, rows = _parse_csv(r.stdout)
    return EngineRun(True, rows, header, r.stdout)


def run_go(gobean: str, q: str) -> EngineRun:
    r = subprocess.run(
        [gobean, "query", "-f", "csv", "-m", str(BEANFILE), q],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    # The Go CLI prints "ERROR: ..." to stdout and still exits 0 for BQL
    # parse/function errors — treat those as failures, not empty tables.
    out = r.stdout or ""
    if r.returncode != 0 or out.lstrip().startswith("ERROR"):
        return EngineRun(False, [], [], (out if out.lstrip().startswith("ERROR") else r.stderr) or out)
    header, rows = _parse_csv(out)
    return EngineRun(True, rows, header, out)


def _num(s: str):
    try:
        return float(s.replace(",", "")) if s not in ("", None) else None
    except (ValueError, AttributeError):
        return None


def _cell_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        if abs(na - nb) <= ABS_TOL:
            return True
        denom = max(abs(na), abs(nb), 1e-12)
        return abs(na - nb) / denom <= REL_TOL
    # Empty (Python drops an inventory that nets to ~0) vs near-zero dust
    # (Go keeps a rounding residual): treat as equal.
    if na is None and nb is not None:
        return abs(nb) <= ABS_TOL
    if nb is None and na is not None:
        return abs(na) <= ABS_TOL
    return False


def _norm_rows(rows: list) -> list:
    return [tuple(_num(c) if _num(c) is not None else c for c in r) for r in rows]


def compare(q: str, py: EngineRun, go: EngineRun) -> tuple[str, list[str]]:
    """Return (status, detail_lines). status in MATCH/MISMATCH/GO_ERROR/PY_ERROR/BOTH_ERROR."""
    detail = []
    if not py.ok and not go.ok:
        return "BOTH_ERROR", [f"py: {py.raw.strip()[:200]}", f"go: {go.raw.strip()[:200]}"]
    if not py.ok:
        return "PY_ERROR", [f"py: {py.raw.strip()[:300]}"]
    if not go.ok:
        return "GO_ERROR", [f"go: {go.raw.strip()[:300]}"]

    # column count / header
    if len(py.header) != len(go.header):
        detail.append(f"header width differs: py={py.header} go={go.header}")
        return "MISMATCH", detail
    if len(py.rows) != len(go.rows):
        detail.append(f"row count differs: py={len(py.rows)} go={len(go.rows)}")

    ordered = "ORDER BY" in q.upper()
    pr, gr = py.rows, go.rows
    if not ordered:
        pr = sorted(pr, key=lambda r: tuple(str(c) for c in r))
        gr = sorted(gr, key=lambda r: tuple(str(c) for c in r))

    mism = 0
    for i in range(min(len(pr), len(gr))):
        prow, grow = pr[i], gr[i]
        if len(prow) != len(grow):
            detail.append(f"row {i}: width py={len(prow)} go={len(grow)}")
            mism += 1
            continue
        for j, (pc, gc) in enumerate(zip(prow, grow)):
            if not _cell_eq(pc, gc):
                detail.append(f"row {i} col {j} ({py.header[j] if j < len(py.header) else '?'}): py={pc!r} go={gc!r}")
                mism += 1
                if mism >= 12:
                    detail.append("... (further diffs truncated)")
                    break
        if mism >= 12:
            break

    if mism == 0 and len(py.rows) == len(go.rows):
        return "MATCH", []
    return "MISMATCH", detail


def main() -> int:
    if not BEANFILE.exists():
        sys.exit(f"beanfile not found: {BEANFILE}")
    if not SF_MONEY.exists():
        sys.exit(f"sf-money not found: {SF_MONEY}")
    gobean = build_go()

    queries = [json.loads(line) for line in QUERIES.read_text().splitlines() if line.strip()]
    print(f"{DIM}ledger: {BEANFILE}{RESET}")
    print(f"{DIM}queries: {len(queries)}{RESET}\n")

    report = [f"Fidelity report — {BEANFILE}", f"queries: {len(queries)}", ""]
    counts = {"MATCH": 0, "MISMATCH": 0, "GO_ERROR": 0, "PY_ERROR": 0, "BOTH_ERROR": 0}

    for spec in queries:
        name, q = spec["name"], spec["q"]
        py = run_python(q)
        go = run_go(gobean, q)
        status, detail = compare(q, py, go)
        counts[status] += 1
        color = {"MATCH": GREEN, "MISMATCH": RED, "GO_ERROR": RED,
                 "PY_ERROR": YELLOW, "BOTH_ERROR": YELLOW}[status]
        print(f"{color}{status:11}{RESET} {name}")
        report.append(f"[{status}] {name}")
        report.append(f"    {q}")
        for d in detail:
            print(f"    {DIM}{d}{RESET}")
            report.append(f"    {d}")
        report.append("")

    print()
    summary = "  ".join(f"{k}={v}" for k, v in counts.items())
    print(summary)
    report.insert(2, summary)
    REPORT.write_text("\n".join(report))
    print(f"{DIM}full report: {REPORT}{RESET}")

    fails = counts["MISMATCH"] + counts["GO_ERROR"] + counts["BOTH_ERROR"]
    return fails


if __name__ == "__main__":
    sys.exit(main())
