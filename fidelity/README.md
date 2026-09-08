# Fidelity harness — Python beanquery vs this Go fork

## Two harnesses here

- **`differential.py` (v3, runs in CI)** — diffs the Go engine against official
  **beancount 3.x** (`beanquery`) over a curated suite (`v3/`) plus the repo's
  compliance query corpus, with a semantic comparator (compares by column
  position, numeric tolerance, order-aware). This is the standing correctness
  gate: the reference tool is the oracle, so every push is checked against real
  beancount v3 behavior (`.github/workflows/differential.yml`). Run locally:
  `BEAN_QUERY=/path/to/bean-query python3 fidelity/differential.py`.
  Current: **12/12 curated + 24 compliance MATCH**.
  - The curated `v3/` suite exercises exactly the locally-patched features —
    CONVERT chains, `COUNT(*)`, per-term `ORDER BY`, `yearmonth`,
    `open_meta`/`getitem`, cost lots, 3-arg `CONVERT`.
  - Known v2/v3 divergences skipped (see `COMPLIANCE_SKIP`): PRINT/JOURNAL/
    BALANCES shortcuts (formatter output, not tabular), `select_star` (column
    set), and `any_meta()` (a v2 function v3 lacks).
  - **Engine gap noted while building the v3 ledger:** the Go engine does not
    elaborate a single blank posting across *multiple* currencies the way v3
    does (an edge case our real ledgers never hit — every txn balances in one
    currency). The fixture uses explicit per-currency legs.

- **`run.py` (real ledger)** — diffs against the *real* sf-money ledger and the
  actual cortex `finance_connect.go` queries. Documented below.

---

# run.py — Python beanquery vs this Go fork (real ledger)

Proves whether this Go fork can stand in for Python `beancount`/`beanquery` on
**Sami's real ledger** (`~/d/code/sf-money/beanfiles/money.bean`), which is the
gate for porting cortex's Finance layer off Python (see
`sf-money/docs/plan/go-port-analysis.md`).

For each query in `queries.jsonl` it runs BOTH engines with identical flags
(`-f csv -m`) and diffs the result tables — numeric cells with a small tolerance,
everything else exact; rows in order when the query has `ORDER BY`, else as a
multiset. Queries are the real ones from `apps/backend/.../finance_connect.go`
plus a few isolating probes.

## Run

```bash
python3 fidelity/run.py                 # builds the Go CLI, needs uv + sf-money checkout
```

Env overrides: `SF_MONEY`, `BEANFILE`, `QUERIES`, `ABS_TOL`, `REL_TOL`.
Exit code = number of non-matching queries. Full details land in `report.txt`.

## Status: 23 / 24 MATCH (2026-09-07)

After the fork patches below: **MATCH 23 · MISMATCH 1 · GO_ERROR 0**. The lone
remaining mismatch (`expenses_monthly_root`) is a cosmetic ORDER BY tie-break:
within one month an empty (~0) group and a −55.69 group swap order — Go sorts 0
before −55.69 under DESC (arguably more correct), Python orders its empty/NULL
inventory differently. Immaterial.

### Fork patches applied to reach this
- **Parser (load the ledger at all):** ignore `#`-at-BOL org/comment lines;
  accept a flag glued to a date (`2023-04-28*`). `parser/lexer.go`, `parser/parser.go`.
- **`COUNT(*)`** — desugar the star to `1` in a call arg (`query/bql/parser.go`).
- **ORDER BY per-term direction** — `date, value DESC` now = date ASC, value DESC
  (was one direction for the whole list). AST + parser + compile + exec.
- **Sort by aggregate value** — `asDecimal` now extracts a scalar from an
  `Amount` / single-currency `Inventory`, so `ORDER BY <CONVERT aggregate>` sorts
  numerically instead of lexically (`query/types.go`).
- **`yearmonth(date)`** — alias of the existing `ymonth` (`query/functions.go`).
- **`open_meta(account)` + `getitem(map, key)`** — the net-worth bucket rollup
  cortex uses (`query/functions.go`).
- **Ledger data fix** (separate, sf-money PR #124): the mis-booked TSLA sale that
  caused the ~€2,720 CONVERT divergence.

Tolerances absorb sub-cent CONVERT rounding (Go keeps full decimal precision,
Python rounds per-step) — immaterial for a finance app.

---

## Findings (2026-09-07, first run — historical)

**MATCH 6 · MISMATCH 5 · GO_ERROR 13** over 24 queries.

Two parser fixes were needed just to load the ledger (both applied to the fork,
with tests): `#`-at-BOL org/comment lines, and a flag glued to a date
(`2023-04-28*`). Both are things Python silently accepts.

### ✅ Proven identical on real data
`SUM` + currency filter, `CONVERT`+`SUM`+`GROUP BY` (all-EUR accounts),
`units()`/`number()`/`cost()`/`currency()`, `CONVERT` of a single position with
multi-column select + `ORDER BY … DESC` + `LIMIT`, and `date_add(today(), -90)`.

### ⚠️ MISMATCH — parses but wrong (priority order)
1. **CONVERT of a mis-booked lot (ROOT CAUSE = LEDGER DATA, not the engine).**
   `wealth_total`, `wealth_eur`, `balances_by_account`, `assets_last_update`
   differ by **~€2 720 (PY 163 895.33 vs GO 161 175.67)** — entirely one account,
   `Assets:Invest:Stocks:TSLA`. A TSLA sale at
   `sf-money/beanfiles/accounts/revolut-investment.bean:1212` was booked
   `-10.01686993 TSLA @ 283.40 USD` (price, no `{}` cost reduction), leaving a
   phantom +10/−10 TSLA pair. Python values the +10 cost lots and leaves the −10
   as an unconvertible residual (→ overcounts, and double-counts the proceeds
   already in RevolutUsd); the fork market-converts the pair to ~0 (closer to
   reality, but by luck, and hides the anomaly). **Neither is trustworthy here.**
   Fix = add `{}` to line 1212 → both engines then agree on the correct figure.
   Migration stance: the fork should MATCH Python's CONVERT (leave-residual) so
   switching engines never silently moves net worth; fix data in the ledger.
2. **ORDER BY on an aliased expression.** `year(date) as date … ORDER BY date`
   orders/aggregates differently between engines (`expenses_yearly_by_root`).

### ❌ GO_ERROR — Go BQL dialect gaps (each is a fork patch)
- **`COUNT(*)`** → `expected expression, found "*"` (5 queries). Needs `COUNT(*)`
  support (or rewrite callers to `COUNT(id)`).
- **`yearmonth(date)`** function not implemented (2 queries).
- **`getitem(open_meta(account), 'bucket')`** not implemented (2 queries — the
  net-worth bucket rollup).
- **`unexpected ","`** on 4 queries — multi-column `GROUP BY year, month` and/or
  `"exclude" IN tags`; needs isolating.

### Takeaway
The engine is a real, correct beanquery for the common cases, but **not yet a
drop-in** for cortex's live queries: ~half need small fork patches (functions +
`COUNT(*)`) and one is a **material numeric divergence** (CONVERT) that must be
reconciled before trusting Go for finance numbers. All are tractable on the fork.
