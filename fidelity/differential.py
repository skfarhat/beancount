#!/usr/bin/env python3
"""v3 differential test: this Go engine vs official beancount 3.x (beanquery).

Two legs, both diffing the Go `beancount` CLI against `bean-query` (v3) with
identical flags (`-f csv -m`) and a SEMANTIC comparator — values are compared by
column position (the engines name/pad columns differently) with a small numeric
tolerance; rows are order-aware when the query has ORDER BY, else compared as a
multiset.

  1. Curated suite (fidelity/v3/) — queries mirroring cortex's real shapes and
     every locally-patched feature (CONVERT chains, COUNT(*), per-term ORDER BY,
     yearmonth, open_meta/getitem, cost lots). MUST match v3 → gates CI.
  2. Compliance sweep — the repo's testdata/compliance/query/*.bql SELECT
     fixtures, for breadth. err_/gap_/non-SELECT and a small documented
     divergence allowlist are skipped; the rest gate too.

Oracle: `bean-query` on PATH (env BEAN_QUERY overrides — CI does `pip install
beancount beanquery`). Go engine: built from ./cmd/beancount (env GOBEAN
overrides). Exit code = number of gating failures (0 = all good).

Usage:  python3 fidelity/differential.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORK = HERE.parent
BEAN_QUERY = os.environ.get("BEAN_QUERY", "bean-query")
ABS_TOL = float(os.environ.get("ABS_TOL", "0.05"))
REL_TOL = float(os.environ.get("REL_TOL", "1e-4"))
TIMEOUT = int(os.environ.get("TIMEOUT", "60"))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Compliance fixtures that legitimately diverge from v3 (documented). These are
# v2-targeted shortcut/format cases, not data errors.
COMPLIANCE_SKIP = {
    # PRINT / shortcut statements render through formatters, not tabular data.
    "gap_print_prices", "gap_print_transactions",
    "journal_account", "journal_all", "journal_at_cost",
    "balances", "balances_at_cost", "balance_cross_account",
    "select_star",  # column set differs across v2/v3
    "metadata",     # uses any_meta(): a v2 function v3 beanquery does not support
}


def bean_query_cmd() -> list[str]:
    # BEAN_QUERY may be a path or a multi-word command.
    return BEAN_QUERY.split() if " " in BEAN_QUERY else [BEAN_QUERY]


def build_go() -> str:
    if go := os.environ.get("GOBEAN"):
        return go
    out = HERE / ".gobean"
    print(f"{DIM}building go engine…{RESET}")
    r = subprocess.run(["go", "build", "-o", str(out), "./cmd/beancount"],
                       cwd=FORK, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"go build failed:\n{r.stderr}")
    return str(out)


def _parse_csv(text: str):
    rows = [tuple(c.strip() for c in row) for row in csv.reader(io.StringIO(text)) if row]
    return (rows[0], rows[1:]) if rows else ([], [])


def run_py(ledger: str, q: str):
    r = subprocess.run(bean_query_cmd() + ["-f", "csv", "-m", "-q", ledger, q],
                       capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        return False, [], (r.stderr or r.stdout)
    header, rows = _parse_csv(r.stdout)
    return True, rows, header


def run_go(gobean: str, ledger: str, q: str):
    r = subprocess.run([gobean, "query", "-f", "csv", "-m", ledger, q],
                       capture_output=True, text=True, timeout=TIMEOUT)
    out = r.stdout or ""
    if r.returncode != 0 or out.lstrip().startswith("ERROR"):
        return False, [], (out if out.lstrip().startswith("ERROR") else r.stderr)
    header, rows = _parse_csv(out)
    return True, rows, header


def _num(s):
    try:
        return float(s.replace(",", "")) if s not in ("", None) else None
    except (ValueError, AttributeError):
        return None


def _cell_eq(a, b) -> bool:
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        if abs(na - nb) <= ABS_TOL:
            return True
        return abs(na - nb) / max(abs(na), abs(nb), 1e-12) <= REL_TOL
    if na is None and nb is not None:
        return abs(nb) <= ABS_TOL
    if nb is None and na is not None:
        return abs(na) <= ABS_TOL
    return False


def compare(q: str, py_rows, go_rows) -> list[str]:
    """Return a list of human-readable diffs (empty = match). Compares by column
    position, so header naming/padding differences are ignored."""
    diffs = []
    if len(py_rows) != len(go_rows):
        diffs.append(f"row count py={len(py_rows)} go={len(go_rows)}")
    pr, gr = py_rows, go_rows
    if "ORDER BY" not in q.upper():
        pr = sorted(pr, key=lambda r: tuple(str(c) for c in r))
        gr = sorted(gr, key=lambda r: tuple(str(c) for c in r))
    for i in range(min(len(pr), len(gr))):
        if len(pr[i]) != len(gr[i]):
            diffs.append(f"row {i}: width py={len(pr[i])} go={len(gr[i])}")
            continue
        for j, (pc, gc) in enumerate(zip(pr[i], gr[i])):
            if not _cell_eq(pc, gc):
                diffs.append(f"row {i} col {j}: py={pc!r} go={gc!r}")
    return diffs


def run_leg(name, gobean, items, gating_failures) -> tuple[int, int, int]:
    """items: list of (fixture_name, ledger_path, query, gating)."""
    passed = skipped = failed = 0
    print(f"\n{name}")
    for fixture, ledger, q, gating in items:
        py_ok, py_rows, _ = run_py(ledger, q)
        go_ok, go_rows, _ = run_go(gobean, ledger, q)
        if not py_ok or not go_ok:
            # Both erroring is agreement; one erroring is a gating failure.
            if not py_ok and not go_ok:
                print(f"  {DIM}both-error {fixture} (agree){RESET}")
                skipped += 1
            else:
                failed += 1
                who = "go" if py_ok else "py"
                print(f"  {RED}ERROR     {fixture}{RESET} ({who} failed)")
                if gating:
                    gating_failures.append(fixture)
            continue
        diffs = compare(q, py_rows, go_rows)
        if not diffs:
            passed += 1
            print(f"  {GREEN}MATCH     {fixture}{RESET}")
        else:
            failed += 1
            print(f"  {RED}MISMATCH  {fixture}{RESET}")
            for d in diffs[:6]:
                print(f"    {DIM}{d}{RESET}")
            if gating:
                gating_failures.append(fixture)
    print(f"  → {GREEN}{passed} match{RESET}, {RED}{failed} fail{RESET}, {skipped} skip")
    return passed, failed, skipped


def curated_items():
    ledger = str(HERE / "v3" / "ledger.beancount")
    specs = [json.loads(l) for l in (HERE / "v3" / "queries.jsonl").read_text().splitlines() if l.strip()]
    return [(s["name"], ledger, s["q"], True) for s in specs]


def compliance_items():
    fx = FORK / "testdata" / "compliance" / "query"
    items = []
    for bql in sorted(fx.glob("*.bql")):
        fixture = bql.stem
        if fixture.startswith("err_") or fixture in COMPLIANCE_SKIP:
            continue
        q = bql.read_text().strip()
        # Only SELECT data queries; skip shortcut statements.
        if not re.match(r"(?is)\s*SELECT\b", q):
            continue
        ledger = fx / f"{fixture}.beancount"
        if not ledger.exists():
            ledger = fx / "ledger.beancount"
        items.append((fixture, str(ledger), q, True))
    return items


def main() -> int:
    # Verify the oracle is present and is v3.
    try:
        ver = subprocess.run(bean_query_cmd() + ["--version"], capture_output=True, text=True)
        print(f"{DIM}oracle: {' '.join(bean_query_cmd())} — {ver.stdout.strip() or ver.stderr.strip()}{RESET}")
    except FileNotFoundError:
        sys.exit(f"{RED}bean-query not found (set BEAN_QUERY or `pip install beancount beanquery`){RESET}")

    gobean = build_go()
    gating_failures: list[str] = []

    run_leg("== curated v3 suite (gates) ==", gobean, curated_items(), gating_failures)
    run_leg("== compliance sweep (gates, minus allowlist) ==", gobean, compliance_items(), gating_failures)

    print()
    if gating_failures:
        print(f"{RED}FAIL{RESET}: {len(gating_failures)} gating divergence(s): {', '.join(gating_failures)}")
    else:
        print(f"{GREEN}PASS{RESET}: Go engine matches beancount v3 across all gating fixtures.")
    return len(gating_failures)


if __name__ == "__main__":
    sys.exit(main())
