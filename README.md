# The Inverted Yield Curve

Is the yield curve inverted, and how long after past inversions did recessions actually
start? Live at [yield.chrislawrence.ca](https://yield.chrislawrence.ca).

No framework, no build step, no package manager. Plain HTML, CSS and vanilla JS on an
nginx front, with a stdlib-Python updater sidecar. Part of the economic tracker
collection (diesel, debt, jobs) and built on the shared
[`econ-core`](https://github.com/Lawrence908/econ-core/blob/main/CONTRACT.md) series contract.

## Layout

```
src/index.html    markup, styling, the TimeChart canvas engine, every render function
data/series.json  machine-fetched, rewritten wholesale each run, never hand-edited
data/meta.json    curated page figures (the NY Fed probability), human-owned
data/recessions.json  vendored from econ-core; never edited here
api/server.py     updater and read-only status API
api/econcore.py   vendored, stamped copy of the shared fetchers
tools/nyfed-check.py  monthly hand-update ritual + construction crosscheck (host-side)
```

## The series

Fifteen series ship in the payload, each on the econ-core contract (id, provenance,
units, freq, confidence, obs pairs). The ones that matter:

- `us_spread_10y3m` / `us_spread_10y2y`: the daily spreads as published by FRED
  (1982 / 1976 onward). Never recomputed here.
- `us_spread_10y3m_monthly`: GS10 minus TB3MS converted from discount to
  bond-equivalent basis (`BEY = 365d/(360-91d)`), monthly from April 1953. This is the
  NY Fed's canonical construction; `tools/nyfed-check.py` verified it against the
  spread column of their own workbook to 5e-5 pp across all 811 overlapping months.
- `us_spread_10y3m_1962_1981`: DGS10 minus BEY(DTB3), the years before a 3-month
  constant-maturity series exists. Different basis, own series, never spliced.
- `ca_spread_10y3m` (daily 2001+, BoC Valet legs) and `ca_spread_10y3m_monthly`
  (1956+, OECD pair whose short leg is interbank, stated wherever drawn).

Computed spreads carry `confidence: estimate` and ship their raw legs alongside so the
arithmetic is checkable.

## The episode table

Computed from the monthly series on every refresh, never hand-maintained. The rule
ships in the payload and prints on the page: an episode is one or more months of
negative monthly-average spread, episodes separated by fewer than six positive months
merge, a recession is credited when an NBER peak falls between the first inverted month
and 24 months after the last, and lead time is measured from the first.

On current data that yields seven credited recessions (1969-2020, median lead 12
months, range 5 to 16), two 1966 false positives, and the 2022-2025 episode open as
"none (yet)". The merged 1978-1981 episode carries both the 1980 and 1981 recessions,
which is the honest reading of the double dip.

## The updater

```bash
docker exec yield-updater python /app/server.py --once      # dry run
docker exec yield-updater python /app/server.py --refresh   # what cron runs
```

Host crontab, twice each weekday after the H.15 afternoon posting, with the log bounded
monthly. Guardrails: stale or shrunken upstreams are kept not written, a failed fetch
carries the previous series forward and records the error, and revisions to
already-published observations land in `changelog.jsonl`. Yields are near-final at
publication, so an entry in that log means Treasury or the Bank of Canada actually
restated a number.

Fetch policy is econ-core's: keyless first (FRED CSV, Valet), keyed FRED as fallback
(`FRED_API_KEY` in `.env`, gitignored). If FRED went dark entirely, the US daily legs
rebuild from Treasury's own par yield curve XML (1990 onward) and the payload says so.

## The NY Fed probability

The one number on the page this repo does not fetch: the NY Fed publishes their
recession probability as a legacy `.xls`, so it lives in curated `meta.json` with a
`last_reviewed` date and the page shows staleness past 45 days. Monthly ritual:

```bash
python3 -m venv /tmp/nyfed-venv && /tmp/nyfed-venv/bin/pip -q install xlrd
/tmp/nyfed-venv/bin/python tools/nyfed-check.py    # prints the values meta.json wants
```

The same run re-verifies our monthly construction against theirs and exits nonzero if
they diverge by more than a basis point.

## Provenance

Assembled with Claude, made by Anthropic. The page reports published spreads, dated
institutional projections, and computed history with the constructions stated; it
forecasts nothing.
