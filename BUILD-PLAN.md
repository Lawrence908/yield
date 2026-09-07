# yield.chrislawrence.ca — build plan

One narrow question: **is the yield curve inverted, and how long after past inversions did
recessions actually start?** Everything on the page serves that question. This is site three
of the family (diesel, debt, yield) and the first one built against the shared series
contract, so decisions made here get inherited by housing, credit, lending and eventually
the econ overlay. Where this plan and laziness disagree, the plan wins.

Written 2026-09-06. Every series below was probed live from daedalus that day; depths are
measured, not assumed.

## Verified sources

| Series | What | Depth (verified) | Freq | Access |
|---|---|---|---|---|
| `T10Y3M` | 10y minus 3m constant maturity, as published by FRED | 1982-01-04 → current | daily | FRED keyed |
| `T10Y2Y` | 10y minus 2y constant maturity | 1976-06-01 → current | daily | FRED keyed |
| `DGS10` / `DGS3MO` | the two legs, levels | 1962 / 1981-09 → | daily | FRED keyed |
| `DTB3` | 3m T-bill secondary market, discount basis | 1954-01-04 → | daily | FRED keyed |
| `GS10` | 10y constant maturity | 1953-04 → | monthly | FRED keyed |
| `TB3MS` | 3m T-bill, discount basis | 1934-01 → | monthly | FRED keyed |
| `USREC` | NBER recession indicator | 1854-12 → | monthly | FRED keyed |
| `BD.CDN.10YR.DQ.YLD` | Canada 10y benchmark | 2001-01-02 → | daily | BoC Valet, keyless |
| `TB.CDN.90D.MID` | Canada 3m T-bill mid-market | 2001-01-02 → | daily | BoC Valet, keyless |
| `IRLTLT01CAM156N` | Canada 10y (OECD MEI) | 1955-01 → | monthly | FRED keyed |
| `IR3TIB01CAM156N` | Canada 3m interbank (OECD MEI) | 1956-01 → | monthly | FRED keyed |

Findings from the probe that override what anyone assumes:

1. **`fredgraph.csv` (the keyless FRED endpoint) is unreachable from this host.** Six of six
   attempts timed out at 90s while `api.stlouisfed.org` answered in 0.2s. So the keyed API is
   primary (`FRED_API_KEY`; debt's key in `/mnt/storage/apps/debt/.env` works, or mint a free
   one), the keyless CSV stays in the code as the documented same-numbers fallback, and the
   **independent** fallback for the US legs is Treasury's own par yield curve XML
   (`home.treasury.gov/...pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=YYYY`,
   keyless, verified working, `BC_10YEAR` and `BC_3MONTH` fields, one file per year, 1990 →).
2. **Valet's daily benchmarks start 2001-01-02, not "the 1980s".** Canada depth comes from
   the OECD monthly pair on FRED instead (1955/1956 →), whose 3m leg is interbank rather
   than T-bill. That basis difference is stated on the page, not smoothed over.
3. ALFRED vintages work with the existing key (probed: UNRATE as published Oct 2008 differs
   from today's revised values). Yield series are barely revised, so vintage display is not a
   v1 feature, but the payload schema carries a `vintage` field from day one so the family
   schema does not change when vintages land on the sites where they matter.
4. The NY Fed publishes its recession-probability model only as a legacy `.xls`
   (`allmonth.xls`, 548KB; the `.csv` URL serves the same xls bytes). No stdlib parse. See
   "NY Fed probability" below.

## Series construction rules

- **Headline: `T10Y3M` as published.** Never recompute what FRED already publishes; the
  spread series are theirs, we chart them. `T10Y2Y` is a toggle, styled as the second-class
  citizen it is (the literature and the launch table both prefer 10y-3m).
- **Deep monthly: `GS10` minus `TB3MS`, from 1953-04, with the 3m leg converted from
  discount to bond-equivalent basis** so the construction matches the NY Fed's canonical
  series: `BEY = 365d / (360 − 91d)` with `d` the discount rate as a decimal. The formula
  prints on the page; the raw `TB3MS` values ship in the payload so the conversion is
  checkable by a reader. One-time build check: open `allmonth.xls` by hand and diff a few
  decades of our monthly spread against their spread column. Near zero this conversion is
  the difference between "inverted" and "not", so it is content, not plumbing.
- **Different basis, own series, never spliced.** `DGS10 − DTB3` gives a daily spread
  1962-1982 before `DGS3MO` exists, but its 3m leg is discount-basis secondary market. It
  renders as its own labelled series in the deep view. Diesel refused to splice the
  refiner-basis monthly panel into the spot dailies; the same rule holds here. The one
  splice diesel does make (ULSD onto heating oil) has no analogue on this site: nothing
  gets spliced.
- **Canada daily** = Valet 10y benchmark minus 3m T-bill mid, 2001 →, keyless, computed by
  us (Valet publishes no spread series; say so). **Canada monthly** = OECD pair, 1956 →,
  labelled "3m interbank" honestly. Canada 2y benchmark exists in Valet if a 10y-2y Canada
  toggle is wanted; optional.
- **Recession shading from `data/recessions.json`, not from markup.** Hardcode NBER peak
  and trough months verbatim from nber.org for the US, and the C.D. Howe Business Cycle
  Council chronology for Canada (no API exists for either; each pair carries a source URL).
  The refresh cross-checks the US pairs against fetched `USREC` and **logs** any mismatch
  (a newly dated cycle) without ever editing the file. Machine verifies, human edits: same
  ownership split debt uses for curated figures.

## The feature: the episode table

Computed from data on every refresh, never hand-maintained. From the monthly bond-equivalent
spread, 1953 →:

- An episode = one or more consecutive months of negative monthly-average spread; episodes
  separated by fewer than 6 positive months merge into one.
- Per episode: first inverted month, trough value and date, months inverted, the next NBER
  recession start (first USREC month after episode start), lead time in months.
- An episode with no recession inside 24 months renders "none within 24m" (1966 shows up
  exactly there, and it stays: the false positive is the honest content).
- The 2022-24 episode, the deepest and longest in the sample, currently renders
  **"none (yet)"**: as of the probe both daily spreads are positive (T10Y3M +0.87,
  T10Y2Y +0.41 on 2026-09-04) and USREC is 0 through 2026-08. That unresolved row is the
  most interesting thing on the page. Do not editorialise it away.
- The detection rule prints beside the table. The rule is content; a different rule gives a
  different table, and the reader deserves to know which one produced this one.

Also derived, same refresh: the status chip (inverted or not, latest value and date, days
since the last inverted trading day, consecutive-days counter while inverted), and a plain
note that historically recessions begin after the curve re-steepens, not at peak inversion,
so an un-inverting curve is not an all-clear.

## NY Fed probability (curated, not fetched)

The one number worth showing that cannot be automated cleanly. Follow debt's `ai-capital`
pattern exactly: a curated JSON entry holding the NY Fed's published 12-month-ahead
recession probability with `value`, `as_of`, `source_url`, `last_reviewed`; the page shows
staleness once `last_reviewed` exceeds ~45 days (it updates monthly, first week). One number
a month by hand beats a fragile xls parser pretending to be automation. **Never compute our
own probit.** Debt's anti-goal holds family-wide: we show documented projections by named
institutions, or nothing.

## Architecture

Clone diesel's shape, not debt's, because this is pure fetch-and-serve with no
human-curated files being machine-edited (the two curated files here, `recessions.json` and
the NY Fed entry, are never written by the updater at all):

- nginx front `yield` (host port **8148**, internal 80) + stdlib-Python sidecar `yield-api`
  (internal 8000, no host port, no site file). One Caddy site file
  (`import proxied yield yield:80`), one services.yml entry.
- In-process wall-clock scheduler, diesel's verbatim: refresh slots **22:30 and 02:30 UTC**
  (H.15 posts ~16:15-18:00 ET; Valet by ~16:30 ET; second slot is the catch-up), retry
  backoff 60s doubling to 30min cap. Weekend runs are harmless no-ops.
- Serve from memory, cache to `data/curve.json`, `user: "1000:1000"`, atomic writes,
  mode 644.
- **Revision log, diesel's differ verbatim**: track the published legs and spreads
  (`T10Y3M`, `T10Y2Y`, `DGS10`, `DGS3MO`, Valet legs, `GS10`, `TB3MS`); skip the diff when
  the source switched between snapshots. Treasury CM yields are near-final at publication,
  so the expected revision rate is low. That makes the log more interesting, not less:
  an entry here means Treasury or the BoC restated a number, which is worth a card.
- Healthcheck probes the dependency, not the process: 503 until a payload exists
  (`wget` not `curl`, `127.0.0.1` not `localhost`).
- `.env`: `FRED_API_KEY` only. Keyless degraded mode must still work: Valet plus Treasury
  XML keep the daily headline alive with FRED down entirely.

### Payload contract (the family standard, first implementation)

Every series in `curve.json` carries:

```json
{ "id": "T10Y3M", "source": "FRED", "source_url": "https://fred.stlouisfed.org/series/T10Y3M",
  "units": "pct_points", "freq": "d", "as_of": "2026-09-04",
  "dates": [...], "values": [...], "vintage": "current", "basis_note": "..." }
```

plus top-level `updated`, `source` (which upstream produced this snapshot), `revisions`
(diesel's shape), `derived` (status chip, episode table), `recessions` (both countries,
from the curated file). `vintage` is `"current"` everywhere in v1; an ALFRED backfill later
adds sibling snapshots without reshaping anything.

**RESOLVED 2026-09-07: the shared kit is `/mnt/storage/apps/econ-core`**, built by the
parallel jobs session (CONTRACT.md, `econcore.py`, the shared `recessions.json`,
`vendor.sh`). yield vendors a stamped copy like every other consumer. The addendum below
records everything in this plan that the landed contract superseded.

## Page

Same bones as diesel: no framework, no build step, no external scripts, hand-rolled canvas
chart class (steal diesel's from `src/index.html`, it already does light and dark via
`prefers-color-scheme`), hub footer copied verbatim from
`proxy/shared-footer/footer.html` with the shared stylesheet linked, not vendored.

Top to bottom:

1. Header: the question, the status chip, freshness line (latest observation date, next
   refresh slot), and the source of the current snapshot.
2. Main chart: daily T10Y3M 1982 → with recession shading, zero line emphasized, T10Y2Y
   toggle, range presets (5y / 20y / max).
3. Deep view: monthly spread 1953 → (bond-equivalent construction), the 1962-82
   `DGS10−DTB3` daily segment as its own labelled series, shading throughout.
4. Episode table, with its rule printed and the "none (yet)" row live.
5. Legs chart: DGS10 and DGS3MO levels, so a reader sees whether the short or the long end
   moved the spread (the 2022 inversion and the 2024-26 re-steepening tell different
   stories through the legs).
6. Canada card: daily spread 2001 →, monthly 1956 →, C.D. Howe shading, basis notes.
7. NY Fed probability card with staleness display.
8. Revisions card (diesel's), sources card listing every series with measured start dates
   and basis caveats, provenance line (assembled with Claude, made by Anthropic), footer.

No numbers in markup, debt's rule: the status line, the episode rows and every figure in
prose interpolate from the payload.

## Deploy checklist (the /new-app skill steps, pinned)

1. Port **8148** (verified free in services.yml and live listeners; keeps the econ family
   adjacent to diesel's 8147; 8149 then 8132-8137 are the next free slots for the family).
2. `sites/yield.caddy`: `import proxied yield yield:80`. Container internal port, not 8148.
3. services.yml entry nested under `services:` (2-space indent), then re-parse with the
   python one-liner. `show_on_landing: true` with a written `public_description`.
4. `./scripts/cf-access.sh create yield.chrislawrence.ca --policy public`, then the retry
   loop until 200; without it the wildcard Access app 302s the site.
5. Kuma block with **compact** keyword `"status":"ok"`, monitor pointed at
   `http://yield:80/api/health` through the front container, then
   `scripts/sync-kuma-monitors.py` dry-run before `--execute`.
6. Screenshots to `screenshots/` (390 mobile fullPage, 1440 desktop) plus the layout audit
   (horizontalScroll false, overflow empty). Playwright paths are relative to
   `/mnt/storage/apps`.
7. `ls -l data/` shows chris-owned readable files before calling it done.
8. Push private: `gh repo create Lawrence908/yield --private --source=. --remote=origin --push`.

## Anti-goals

- No house recession model, no probability we computed ourselves, no forecast language.
  The page reports measured spreads, dated institutional projections, and history.
- No splicing series with different bases onto one line, ever. Separate labelled series.
- No framework, bundler, package manager, or CDN script tag.
- No hand-maintained derived data: if the refresh can compute it (episode table, status
  chip, lead times), the refresh computes it.
- No faked automation: the NY Fed number is manual and says so via `last_reviewed`.
- No silently overwriting `recessions.json`: the updater logs mismatches, a human edits.
- No emdashes in page copy.

## Acceptance

- Page renders end to end from `data/curve.json` with zero console errors, both themes,
  380px and 1440px, keyboard focus visible, `prefers-reduced-motion` respected.
- Kill `FRED_API_KEY`: the daily headline still updates via Valet + Treasury XML, the page
  says which source produced the snapshot, and nothing renders as if nothing happened.
- The episode table reproduces the canonical record: every recession since 1957 preceded by
  an inversion, 1966 shown as the false positive, 2022-24 shown unresolved.
- The monthly spread matches the NY Fed's published spread column (hand check against
  `allmonth.xls`) within rounding across at least three decades.
- A reader can trace any figure on the page to a source URL in two clicks.
- `docker ps` shows both containers `(healthy)`, Kuma monitor green, Access bypass live,
  screenshots committed, repo pushed, `git status` clean with no `.env` and no
  machine-owned data files.

## Addendum, 2026-09-07: alignment with econ-core (written as built)

`econ-core` and `jobs` landed from the parallel session mid-build, and this plan was
aligned to them rather than the reverse. What changed against the sections above:

1. **Architecture is the jobs shape, not the diesel shape.** Host cron calling
   `docker exec yield-updater ... --refresh` (one scheduler, visible in `crontab -l`),
   payload composed from disk and cached on mtime, machine-owned `series.json` rewritten
   wholesale, curated `meta.json` and vendored `recessions.json` never machine-touched.
   The in-process wall-clock scheduler described above was diesel's pattern and was not
   built. Cron runs twice each weekday after the H.15 afternoon posting.
2. **The fredgraph timeout was a User-Agent tarpit, not a block.** fred.stlouisfed.org
   answers curl/wget/urllib default UAs instantly and tarpits browser-imitating ones from
   non-browser TLS stacks. econcore sends the default UA, so the contract's
   keyless-primary policy stands; the keyed API is the fallback and the vintage route.
   Treasury XML remains implemented as the truly independent US fallback, engaged only
   when every FRED route fails and the stored copy is stale.
3. **Series shapes follow CONTRACT.md**: `obs` as `[date, value]` pairs, snake_case ids
   (`us_spread_10y3m`, `us_spread_10y3m_monthly`, `ca_spread_10y3m`, ...), computed
   spreads carry `confidence: estimate` with the construction stated, raw legs ship
   alongside. Recession bands come from the vendored shared dataset, drawn peak month
   through trough inclusive.
4. **The episode table is computed, and the first real run rewrote two assertions in
   this plan.** On the canonical construction no inversion precedes the 1957 or 1960
   recessions, so the honest claim is "every recession since 1969", matching the NY
   Fed's own episode list; and the 2022 episode's monthly series re-inverted through
   April 2025, making it 27 months long with the attribution window open until 2027.
   The acceptance line "every recession since 1957" above is superseded by the computed
   table: 7 credited recessions 1969-2020, median lead 12 months, range 5 to 16, two
   1966 false positives, one row pending.
5. **NY Fed values seeded from the workbook** (July 2026 data, 15.2% by July 2027) and
   the BEY formula verified against their own bond-equivalent column to 4e-15 across 811
   rows. `tools/nyfed-check.py` is the monthly hand-update ritual and the construction
   crosscheck; it needs xlrd, so it runs host-side, never in the container.
