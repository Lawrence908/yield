#!/usr/bin/env python3
"""yield.chrislawrence.ca data updater and read-only status API.

One narrow question: is the yield curve inverted, and how long after past
inversions did recessions actually start? Everything served here feeds that.

Everything live on the page comes from series.json, machine-owned and
rewritten wholesale each run. The curated files (meta.json with the NY Fed
probability, recessions.json vendored from econ-core) are never touched by
automation.

Guardrails on the machine-owned side, inherited from jobs:

  * a series whose newest observation is older than the stored one is kept,
    not replaced;
  * a series that shrinks by more than 10% is kept, not replaced;
  * a fetch failure carries the previous data forward and records the error,
    so one dead endpoint degrades one chart instead of blanking the site;
  * revisions to already-published observations are appended to
    changelog.jsonl. Treasury constant-maturity yields are near-final at
    publication, so an entry here means Treasury or the Bank of Canada
    actually restated a number, which is worth a card.

The derived spreads and the episode table are recomputed from shipped inputs
on every run and never hand-maintained. The detection rule ships in the
payload so the page prints the rule that produced the table.

HTTP here is read-only. Runs happen via host cron calling
`docker exec yield-updater python /app/server.py --refresh`.
"""

import json
import os
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import econcore

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

SERIES_FILE = os.path.join(DATA_DIR, "series.json")
CHANGELOG = os.path.join(DATA_DIR, "changelog.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "updater-state.json")
RECESSIONS_FILE = os.path.join(DATA_DIR, "recessions.json")

CURATED = ["meta", "recessions"]
SHRINK_TOLERANCE = 0.9
CHANGELOG_IN_PAYLOAD = 100

# The episode rule. Ships in the payload so the page states the rule that
# produced the table; a different rule gives a different table.
EPISODE_RULE = {
    "series": "us_spread_10y3m_monthly",
    "threshold": 0.0,
    "merge_gap_months": 6,
    "window_months": 24,
    "statement": ("An inversion episode is one or more months of negative "
                  "monthly-average spread; episodes separated by fewer than "
                  "six positive months merge into one. An episode is credited "
                  "with a recession when an NBER peak falls between its first "
                  "inverted month and 24 months after its last; lead time is "
                  "measured from the first."),
}

TREASURY_XML = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/pages/xml?data=daily_treasury_yield_curve"
                "&field_tdr_date_value={}")
TREASURY_FIRST_YEAR = 1990

_payload_cache = {"stamp": None, "body": None}
_state = {"last_run": None, "results": []}
_lock = threading.Lock()


def bond_equivalent(discount_pct):
    """Convert a 3-month discount-basis rate to bond-equivalent yield.

    BEY = 365 d / (360 - 91 d), d as a decimal. This is the NY Fed's own
    construction for their canonical spread; verified against the BEY column
    of their allmonth.xls to 4e-15 across 811 monthly rows (2026-09-07).
    Constant-maturity series (DGS*, GS10) are already bond-equivalent and
    must not be passed through this.
    """
    d = discount_pct / 100.0
    return 365.0 * d / (360.0 - 91.0 * d) * 100.0


# --------------------------------------------------------------------------
# the series list
#
# Adding a series is a human decision with a verified source; the updater
# only refreshes what is declared. Depths in the notes were probed live on
# 2026-09-06, not assumed.
# --------------------------------------------------------------------------

def _fred(series_id):
    return lambda: econcore.fred_series(series_id, FRED_KEY)


def _fred_window(series_id, lo, hi):
    return lambda: [o for o in econcore.fred_series(series_id, FRED_KEY)
                    if lo <= o[0] <= hi]


def _valet(series_name):
    return lambda: econcore.valet_series(series_name)


FETCHED = [
    {
        "id": "us_spread_10y3m",
        "fetch": _fred("T10Y3M"),
        "label": "US 10-year minus 3-month spread",
        "source": "Federal Reserve H.15 constant-maturity yields, spread as published by FRED T10Y3M",
        "source_url": "https://fred.stlouisfed.org/series/T10Y3M",
        "units": "percentage_points", "freq": "daily",
        "note": "10-year minus 3-month Treasury constant maturity, both bond-equivalent. Daily since 1982-01-04, bounded by the 3-month constant-maturity series. The single best-documented recession predictor; published, not computed here.",
    },
    {
        "id": "us_spread_10y2y",
        "fetch": _fred("T10Y2Y"),
        "label": "US 10-year minus 2-year spread",
        "source": "Federal Reserve H.15 constant-maturity yields, spread as published by FRED T10Y2Y",
        "source_url": "https://fred.stlouisfed.org/series/T10Y2Y",
        "units": "percentage_points", "freq": "daily",
        "note": "The spread the headlines quote. Daily since 1976-06-01. Research (and the NY Fed) prefer 10y-3m; this one is here because everyone asks.",
    },
    {
        "id": "us_10y",
        "fetch": _fred("DGS10"),
        "label": "US 10-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS10",
        "source_url": "https://fred.stlouisfed.org/series/DGS10",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1962-01-02. One leg of the spread; the legs say whether the short or the long end moved it.",
    },
    {
        "id": "us_3m",
        "fetch": _fred("DGS3MO"),
        "label": "US 3-month Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS3MO",
        "source_url": "https://fred.stlouisfed.org/series/DGS3MO",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1981-09-01, which is why the published daily spread starts in 1982.",
    },
    {
        "id": "us_30y",
        "fetch": _fred("DGS30"),
        "label": "US 30-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS30",
        "source_url": "https://fred.stlouisfed.org/series/DGS30",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1977-02-15. Treasury stopped issuing the 30-year in February 2002 and resumed in February 2006; the current FRED vintage carries values through that window, built from the Treasury's long-term extrapolation factor rather than an auctioned 30-year bond. Drawn only behind the legs chart's All tenors toggle.",
    },
    {
        "id": "us_5y",
        "fetch": _fred("DGS5"),
        "label": "US 5-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS5",
        "source_url": "https://fred.stlouisfed.org/series/DGS5",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1962-01-02. Drawn only behind the legs chart's All tenors toggle.",
    },
    {
        "id": "us_3y",
        "fetch": _fred("DGS3"),
        "label": "US 3-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS3",
        "source_url": "https://fred.stlouisfed.org/series/DGS3",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1962-01-02. Drawn only behind the legs chart's All tenors toggle.",
    },
    {
        "id": "us_1y",
        "fetch": _fred("DGS1"),
        "label": "US 1-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS1",
        "source_url": "https://fred.stlouisfed.org/series/DGS1",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1962-01-02. Drawn only behind the legs chart's All tenors toggle.",
    },
    {
        "id": "us_2y",
        "fetch": _fred("DGS2"),
        "label": "US 2-year Treasury yield",
        "source": "Federal Reserve H.15, via FRED DGS2",
        "source_url": "https://fred.stlouisfed.org/series/DGS2",
        "units": "percent", "freq": "daily",
        "note": "Constant maturity, daily since 1976-06-01. The short leg of the 10y-2y spread published as T10Y2Y, shipped so that spread's arithmetic is checkable against its own legs, and the short line on the legs chart.",
    },
    {
        "id": "us_10y_monthly",
        "fetch": _fred("GS10"),
        "label": "US 10-year yield, monthly",
        "source": "Federal Reserve H.15, via FRED GS10",
        "source_url": "https://fred.stlouisfed.org/series/GS10",
        "units": "percent", "freq": "monthly",
        "note": "Monthly average of daily constant-maturity yields, since April 1953. Long leg of the deep spread.",
    },
    {
        "id": "us_3m_tbill_monthly",
        "fetch": _fred("TB3MS"),
        "label": "US 3-month T-bill rate, monthly",
        "source": "Federal Reserve H.15, via FRED TB3MS",
        "source_url": "https://fred.stlouisfed.org/series/TB3MS",
        "units": "percent", "freq": "monthly",
        "note": "Secondary-market rate on a DISCOUNT basis, since January 1934. Shipped raw so the bond-equivalent conversion used in the deep spread is checkable against it.",
    },
    {
        "id": "us_3m_tbill_daily_1962_1981",
        "fetch": _fred_window("DTB3", "1962-01-01", "1981-12-31"),
        "label": "US 3-month T-bill rate, daily, 1962-1981 window",
        "source": "Federal Reserve H.15, via FRED DTB3 (window)",
        "source_url": "https://fred.stlouisfed.org/series/DTB3",
        "units": "percent", "freq": "daily",
        "note": "Secondary-market discount-basis rate, deliberately windowed to the years before DGS3MO exists. Input to the pre-1982 spread segment; the full series reaches back to 1954.",
    },
    {
        "id": "ca_10y",
        "fetch": _valet("BD.CDN.10YR.DQ.YLD"),
        "label": "Canada 10-year benchmark yield",
        "source": "Bank of Canada Valet, series BD.CDN.10YR.DQ.YLD",
        "source_url": "https://www.bankofcanada.ca/valet/series/BD.CDN.10YR.DQ.YLD",
        "units": "percent", "freq": "daily",
        "note": "Government of Canada 10-year benchmark bond, daily. Valet's daily history begins 2001-01-02; that is the platform's depth, not the bond's.",
    },
    {
        "id": "ca_3m",
        "fetch": _valet("TB.CDN.90D.MID"),
        "label": "Canada 3-month T-bill yield",
        "source": "Bank of Canada Valet, series TB.CDN.90D.MID",
        "source_url": "https://www.bankofcanada.ca/valet/series/TB.CDN.90D.MID",
        "units": "percent", "freq": "daily",
        "note": "3-month treasury bill mid-market yield, daily since 2001-01-02 on Valet.",
    },
    {
        "id": "ca_10y_monthly",
        "fetch": _fred("IRLTLT01CAM156N"),
        "label": "Canada 10-year yield, monthly",
        "source": "OECD Main Economic Indicators, via FRED IRLTLT01CAM156N",
        "source_url": "https://fred.stlouisfed.org/series/IRLTLT01CAM156N",
        "units": "percent", "freq": "monthly",
        "note": "Long-term government bond yield, monthly since January 1955. The deep Canadian leg.",
    },
    {
        "id": "ca_3m_interbank_monthly",
        "fetch": _fred("IR3TIB01CAM156N"),
        "label": "Canada 3-month interbank rate, monthly",
        "source": "OECD Main Economic Indicators, via FRED IR3TIB01CAM156N",
        "source_url": "https://fred.stlouisfed.org/series/IR3TIB01CAM156N",
        "units": "percent", "freq": "monthly",
        "note": "INTERBANK rate, not a T-bill: the OECD's short leg for Canada. Monthly since January 1956. The basis difference against the daily T-bill spread is stated on the page, not smoothed over.",
    },
]


# --------------------------------------------------------------------------
# US Treasury par-yield fallback
#
# Independent of FRED entirely: Treasury's own par yield curve XML, keyless,
# one file per year from 1990. Used only when every FRED route for the US
# daily legs failed and the stored copy is stale or absent; a working FRED
# with a revoked key already degrades to the keyless CSV inside econcore.
# --------------------------------------------------------------------------

def _treasury_year(year):
    """{iso_date: (y3m, y10)} for one calendar year of par yields."""
    body = econcore._get(TREASURY_XML.format(year), timeout=90)  # noqa: SLF001 - shared transport
    out = {}
    for entry in ET.fromstring(body).iter():
        if not entry.tag.endswith("}entry"):
            continue
        row = {}
        for leaf in entry.iter():
            tag = leaf.tag.rsplit("}", 1)[-1]
            if tag in ("NEW_DATE", "BC_3MONTH", "BC_10YEAR"):
                row[tag] = (leaf.text or "").strip()
        try:
            date = row["NEW_DATE"][:10]
            out[date] = (float(row["BC_3MONTH"]), float(row["BC_10YEAR"]))
        except (KeyError, ValueError):
            continue
    return out

def treasury_fallback_series():
    """(us_3m, us_10y, spread) contract series built from Treasury XML."""
    this_year = datetime.now(timezone.utc).year
    rows, failed = {}, []
    for year in range(TREASURY_FIRST_YEAR, this_year + 1):
        try:
            rows.update(_treasury_year(year))
        except Exception as exc:  # noqa: BLE001 - a missing old year is tolerable
            failed.append("%s (%s)" % (year, type(exc).__name__))
    if not rows or max(rows)[:4] != str(this_year):
        raise ValueError("treasury fallback incomplete: %d rows, failures %s"
                         % (len(rows), failed or "none"))
    dates = sorted(rows)
    note = ("FALLBACK SNAPSHOT: fetched from Treasury's par yield curve XML "
            "because FRED was unreachable. Same par-yield data H.15 "
            "publishes, but 1990 onward only." +
            (" Years failed: %s." % ", ".join(failed) if failed else ""))
    src = "US Treasury daily par yield curve XML"
    url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"
    y3 = econcore.make_series("us_3m", "US 3-month Treasury yield", src, url,
                              "percent", "daily",
                              [[d, rows[d][0]] for d in dates], note=note)
    y10 = econcore.make_series("us_10y", "US 10-year Treasury yield", src, url,
                               "percent", "daily",
                               [[d, rows[d][1]] for d in dates], note=note)
    sp = econcore.make_series("us_spread_10y3m",
                              "US 10-year minus 3-month spread", src, url,
                              "percentage_points", "daily",
                              [[d, round(rows[d][1] - rows[d][0], 4)] for d in dates],
                              confidence="estimate",
                              note=note + " Spread computed here from the two legs.")
    return {"us_3m": y3, "us_10y": y10, "us_spread_10y3m": sp}


# --------------------------------------------------------------------------
# derived series: the constructions, stated
# --------------------------------------------------------------------------

def _aligned_spread(long_obs, short_obs, convert_short=None, digits=4):
    """[[date, long - short], ...] on the dates both legs share."""
    short_map = dict(map(tuple, short_obs))
    out = []
    for date, long_v in long_obs:
        short_v = short_map.get(date)
        if short_v is None:
            continue
        if convert_short:
            short_v = convert_short(short_v)
        out.append([date, round(long_v - short_v, digits)])
    return out


def build_derived(series):
    """Computed from shipped inputs on every run. Confidence is 'estimate'
    because the arithmetic happens here; every input is a reported series in
    the same payload, so a reader can check it."""
    out = {}
    ycfaq = "https://www.newyorkfed.org/research/capital_markets/ycfaq"

    gs10 = series.get("us_10y_monthly")
    tb3 = series.get("us_3m_tbill_monthly")
    if gs10 and tb3:
        out["us_spread_10y3m_monthly"] = econcore.make_series(
            "us_spread_10y3m_monthly",
            "US 10-year minus 3-month spread, monthly, 1953 onward",
            "Derived: FRED GS10 minus TB3MS converted to bond-equivalent basis, the NY Fed's canonical construction",
            ycfaq, "percentage_points", "monthly",
            _aligned_spread(gs10["obs"], tb3["obs"], bond_equivalent),
            confidence="estimate",
            note="BEY = 365d/(360-91d) applied to the discount-basis bill rate before subtracting; near zero that conversion is the difference between inverted and not. Both raw legs ship in this payload. Matches the spread column of the NY Fed's allmonth.xls over their 1959-onward sample.")

    dgs10 = series.get("us_10y")
    dtb3 = series.get("us_3m_tbill_daily_1962_1981")
    if dgs10 and dtb3:
        out["us_spread_10y3m_1962_1981"] = econcore.make_series(
            "us_spread_10y3m_1962_1981",
            "US 10-year minus 3-month spread, daily, 1962-1981 segment",
            "Derived: FRED DGS10 minus DTB3 converted to bond-equivalent basis",
            "https://fred.stlouisfed.org/series/DTB3",
            "percentage_points", "daily",
            _aligned_spread(dgs10["obs"], dtb3["obs"], bond_equivalent),
            confidence="estimate",
            note="The years before a 3-month constant-maturity series exists. The short leg is the secondary-market discount rate, a different basis from the published post-1982 spread, so this is its own labelled series and is never spliced onto the headline line.")

    ca10 = series.get("ca_10y")
    ca3 = series.get("ca_3m")
    if ca10 and ca3:
        out["ca_spread_10y3m"] = econcore.make_series(
            "ca_spread_10y3m",
            "Canada 10-year minus 3-month spread",
            "Derived: Bank of Canada Valet 10-year benchmark minus 3-month T-bill mid",
            "https://www.bankofcanada.ca/valet/docs",
            "percentage_points", "daily",
            _aligned_spread(ca10["obs"], ca3["obs"]),
            confidence="estimate",
            note="The Bank publishes the legs, not the spread; subtraction happens here. Both legs ship in this payload. Daily from 2001, Valet's depth.")

    ca10m = series.get("ca_10y_monthly")
    ca3m = series.get("ca_3m_interbank_monthly")
    if ca10m and ca3m:
        out["ca_spread_10y3m_monthly"] = econcore.make_series(
            "ca_spread_10y3m_monthly",
            "Canada long minus 3-month interbank spread, monthly, 1956 onward",
            "Derived: OECD MEI long-term yield minus 3-month interbank rate, via FRED",
            "https://fred.stlouisfed.org/series/IR3TIB01CAM156N",
            "percentage_points", "monthly",
            _aligned_spread(ca10m["obs"], ca3m["obs"]),
            confidence="estimate",
            note="The short leg is an INTERBANK rate, not a T-bill, so this runs a touch above a true bill spread and is drawn as its own series beside the daily one, never spliced.")

    return out


# --------------------------------------------------------------------------
# analysis: status chip and the episode table
# --------------------------------------------------------------------------

def _spread_status(entry):
    obs = entry["obs"]
    latest_date, latest_value = obs[-1]
    inverted = latest_value < 0
    last_negative = None
    for date, value in reversed(obs):
        if value < 0:
            last_negative = date
            break
    streak = 0
    for _, value in reversed(obs):
        if (value < 0) == inverted:
            streak += 1
        else:
            break
    first_streak_date = obs[-streak][0] if streak else latest_date
    return {
        "latest": [latest_date, latest_value],
        "inverted": inverted,
        "last_negative": last_negative,
        "streak_obs": streak,
        "streak_since": first_streak_date,
    }


def _mi(year_month):
    year, month = year_month.split("-")[:2]
    return int(year) * 12 + int(month) - 1


def build_episodes(monthly_entry, recessions):
    """The table, computed. Never hand-maintained: a different rule gives a
    different table, so the rule ships alongside the rows."""
    months = [[date[:7], value] for date, value in monthly_entry["obs"]]
    negatives = [i for i, (_, value) in enumerate(months)
                 if value < EPISODE_RULE["threshold"]]
    if not negatives:
        return {"rule": EPISODE_RULE, "episodes": [], "stats": {}}

    # group, merging gaps shorter than the rule allows
    groups = [[negatives[0], negatives[0]]]
    for i in negatives[1:]:
        prev_end = groups[-1][1]
        gap = _mi(months[i][0]) - _mi(months[prev_end][0]) - 1
        if gap < EPISODE_RULE["merge_gap_months"]:
            groups[-1][1] = i
        else:
            groups.append([i, i])

    bands = recessions["us"]["bands"]
    data_through = _mi(recessions["us"]["as_of"][:7])
    used_peaks = set()
    episodes = []
    for start_i, end_i in groups:
        span = months[start_i:end_i + 1]
        start, end = span[0][0], span[-1][0]
        trough = min(span, key=lambda m: m[1])
        window_end = _mi(end) + EPISODE_RULE["window_months"]
        peaks = []
        for band in bands:
            peak = band["peak"]
            if peak in used_peaks:
                continue
            if _mi(start) <= _mi(peak) <= window_end:
                peaks.append(peak)
                used_peaks.add(peak)
        if peaks:
            outcome = "recession"
        elif window_end > data_through:
            outcome = "pending"
        else:
            outcome = "none_in_window"
        episodes.append({
            "start": start,
            "end": end,
            "months_inverted": len([1 for _, value in span
                                    if value < EPISODE_RULE["threshold"]]),
            "trough": {"month": trough[0], "value": trough[1]},
            "recessions": peaks,
            "lead_months": (_mi(peaks[0]) - _mi(start)) if peaks else None,
            "outcome": outcome,
        })

    leads = sorted(e["lead_months"] for e in episodes
                   if e["lead_months"] is not None)
    stats = {}
    if leads:
        mid = len(leads) // 2
        median = (leads[mid] if len(leads) % 2
                  else (leads[mid - 1] + leads[mid]) / 2.0)
        stats = {"credited_episodes": len(leads),
                 "median_lead_months": median,
                 "min_lead_months": leads[0],
                 "max_lead_months": leads[-1],
                 "false_positives": len([e for e in episodes
                                         if e["outcome"] == "none_in_window"]),
                 "pending": len([e for e in episodes
                                 if e["outcome"] == "pending"])}
    return {"rule": EPISODE_RULE, "episodes": episodes, "stats": stats}


MONTH_WORDS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

CHIP_RULE = ("The curve is inverted when the 10-year yield sits below the "
             "3-month yield, so the spread is negative.")


def _day_word(iso):
    parts = iso.split("-")
    return "%s %d, %s" % (MONTH_WORDS[int(parts[1]) - 1], int(parts[2]), parts[0])


def _signed(value, places=2):
    """The page's sign convention: U+2212 for negatives, never a hyphen."""
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return "%s%.*f" % (sign, places, abs(value))


def _headline(status):
    """The one-line state, keyed on 10y−3m: the spread with the record."""
    spread = status.get("us_spread_10y3m")
    if not spread:
        return None
    inverted = bool(spread["inverted"])
    if inverted:
        detail = "%s pp · %d trading days and counting" % (
            _signed(spread["latest"][1]), spread["streak_obs"])
    else:
        detail = "%s pp on %s" % (_signed(spread["latest"][1]),
                                  _day_word(spread["latest"][0]))
        if spread.get("last_negative"):
            detail += " · positive for %d trading days" % spread["streak_obs"]
    return {
        "state": "signal" if inverted else "normal",
        "label": "Inverted" if inverted else "Not inverted",
        "detail": "10y−3m " + detail,
        "as_of": spread["latest"][0],
        "rule": CHIP_RULE,
    }


def build_status(series):
    status = {}
    for sid in ("us_spread_10y3m", "us_spread_10y2y", "ca_spread_10y3m"):
        if sid in series:
            status[sid] = _spread_status(series[sid])
    head = _headline(status)
    if head:
        status["headline"] = head
        status["signal_active"] = head["state"] == "signal"
    return status


def build_analysis(series):
    analysis = {"status": build_status(series)}
    monthly = series.get("us_spread_10y3m_monthly")
    if monthly:
        try:
            recessions = econcore.load_recessions(RECESSIONS_FILE)
            analysis["episodes"] = build_episodes(monthly, recessions)
        except Exception as exc:  # noqa: BLE001 - the table degrades, the page renders
            analysis["episodes_error"] = "%s: %s" % (type(exc).__name__, exc)
    return analysis


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------

def load_old_series():
    try:
        with open(SERIES_FILE) as fh:
            return json.load(fh).get("series", {})
    except Exception:  # noqa: BLE001 - first run, or corrupt file: start clean
        return {}


def _diff_revisions(series_id, old_obs, new_obs):
    """Changed values at already-published dates, raw fetched legs only.
    Derived spreads move when a leg moves; diffing them too would report the
    same restatement several times (diesel's rule)."""
    old_map = dict(map(tuple, old_obs))
    changed = [(d, old_map[d], v) for d, v in new_obs
               if d in old_map and abs(old_map[d] - v) > 1e-9]
    if not changed:
        return None
    deltas = [abs(after - before) for _, before, after in changed]
    return {
        "series": series_id, "action": "revised",
        "changed": len(changed),
        "span": [changed[0][0], changed[-1][0]],
        "max_delta": round(max(deltas), 4),
        "sample": [{"date": d, "before": b, "after": a}
                   for d, b, a in changed[:3]],
    }


def refresh_series(dry=False):
    old = load_old_series()
    series, errors, results = {}, {}, []

    for spec in FETCHED:
        sid = spec["id"]
        prev = old.get(sid)
        rec = {"series": sid, "action": "fetched"}
        try:
            obs = spec["fetch"]()
            doc = econcore.make_series(
                sid, spec["label"], spec["source"], spec["source_url"],
                spec["units"], spec["freq"], obs, note=spec.get("note"))
            if prev and prev.get("obs"):
                if doc["as_of"] < prev["as_of"]:
                    rec.update(action="stale-upstream",
                               reason="upstream at %s, behind stored %s; kept"
                                      % (doc["as_of"], prev["as_of"]))
                    doc = prev
                elif len(obs) < len(prev["obs"]) * SHRINK_TOLERANCE:
                    rec.update(action="shrunk",
                               reason="%d obs against %d stored; kept"
                                      % (len(obs), len(prev["obs"])))
                    doc = prev
                else:
                    revision = _diff_revisions(sid, prev["obs"], obs)
                    if revision and prev.get("source") == doc.get("source"):
                        if not dry:
                            econcore.log_revision(CHANGELOG, revision)
                        rec.update(action="revised",
                                   changed=revision["changed"])
                    added = len(obs) - len(prev["obs"])
                    if added > 0:
                        rec["added"] = added
            series[sid] = doc
        except Exception as exc:  # noqa: BLE001 - one dead endpoint, one chart
            errors[sid] = "%s: %s" % (type(exc).__name__, exc)
            rec.update(action="error", reason=errors[sid])
            if prev:
                series[sid] = prev
                rec["carried_forward"] = True
        results.append(rec)
        print("%-32s %-14s %s" % (sid, rec["action"], rec.get("reason", "")),
              flush=True)

    # Treasury fallback: only when FRED gave nothing for the US daily legs
    # and there is nothing usable, or only week-old data, to carry forward.
    us_daily = ("us_spread_10y3m", "us_10y", "us_3m")
    if all(sid in errors for sid in us_daily):
        newest = max((series[sid]["as_of"] for sid in us_daily
                      if sid in series), default=None)
        horizon = (datetime.now(timezone.utc).date()
                   .toordinal() - 7)
        stale = (newest is None
                 or datetime.strptime(newest, "%Y-%m-%d").date().toordinal() < horizon)
        if stale:
            try:
                replacement = treasury_fallback_series()
                series.update(replacement)
                results.append({"series": "us_daily_legs",
                                "action": "treasury-fallback",
                                "reason": "FRED unreachable; rebuilt 1990+ from Treasury XML"})
                print("treasury fallback engaged", flush=True)
            except Exception as exc:  # noqa: BLE001 - fallback of a fallback: report
                errors["treasury_fallback"] = "%s: %s" % (type(exc).__name__, exc)
                print("treasury fallback failed: %s" % errors["treasury_fallback"],
                      flush=True)

    if not series:
        raise ValueError("nothing fetched and nothing stored; refusing to write")

    series.update(build_derived(series))
    analysis = build_analysis(series)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "Machine-fetched. Never hand-edited; the updater rewrites this file wholesale.",
        "econcore": econcore.VERSION,
        "fred_key_used": bool(FRED_KEY),
        "errors": errors,
        "series": series,
        "analysis": analysis,
    }

    if dry:
        total = sum(len(s["obs"]) for s in series.values())
        print("dry run: %d series, %d observations, %d errors -- not written"
              % (len(series), total, len(errors)), flush=True)
        return payload

    tmp = SERIES_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, SERIES_FILE)

    with _lock:
        _state["last_run"] = datetime.now(timezone.utc).isoformat()
        _state["results"] = results
    _save_state()

    total = sum(len(s["obs"]) for s in series.values())
    print("series refreshed: %d series, %d observations, %d errors"
          % (len(series), total, len(errors)), flush=True)
    return payload


def _save_state():
    try:
        with _lock:
            snapshot = dict(_state)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# read-only HTTP
# --------------------------------------------------------------------------

def _load(name):
    with open(os.path.join(DATA_DIR, name)) as fh:
        return json.load(fh)


def data_stamp():
    newest = 0.0
    names = [n + ".json" for n in CURATED] + ["series.json", "changelog.jsonl"]
    for name in names:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(DATA_DIR, name)))
        except OSError:
            continue
    return newest


def build_data_payload():
    """Composed from disk, cached on mtime: the refresh runs outside this
    process via docker exec, so an in-memory payload would keep serving
    superseded figures behind a healthy endpoint."""
    stamp = data_stamp()
    if _payload_cache["stamp"] == stamp and _payload_cache["body"] is not None:
        return _payload_cache["body"]

    payload = {"generated_at": datetime.now(timezone.utc).isoformat()}
    for name in CURATED:
        try:
            payload[name] = _load(name + ".json")
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            payload[name] = None
            payload.setdefault("errors", {})[name] = str(exc)
    try:
        doc = _load("series.json")
        payload["series"] = doc.get("series", {})
        # The stored block is written by the refresh, which runs out of
        # process; one written before the status contract existed has no
        # headline, and the chip would stay hidden until the next scheduled
        # run. Recomputing the cheap half here makes a deploy take effect now.
        analysis = dict(doc.get("analysis", {}))
        if "headline" not in (analysis.get("status") or {}):
            analysis["status"] = build_status(payload["series"])
        payload["analysis"] = analysis
        payload["series_fetched_at"] = doc.get("fetched_at")
        payload["series_errors"] = doc.get("errors", {})
    except Exception as exc:  # noqa: BLE001 - charts degrade, page renders
        payload["series"] = {}
        payload["analysis"] = {}
        payload.setdefault("errors", {})["series"] = str(exc)

    recent, total = econcore.read_revisions(CHANGELOG, CHANGELOG_IN_PAYLOAD)
    payload["changelog"] = {"total": total, "recent": recent}

    _payload_cache["stamp"] = stamp
    _payload_cache["body"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, cache="no-cache"):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            # Probe the dependency, not the process: no data, not healthy.
            try:
                doc = _load("series.json")
                spread = doc.get("series", {}).get("us_spread_10y3m", {})
                st = doc.get("analysis", {}).get("status", {})
                # A stored block written before the status contract existed
                # has no headline; recompute so health and /api/data agree
                # rather than the hub seeing one and the page the other.
                if "headline" not in st:
                    st = build_status(doc.get("series", {}))
                status = st.get("us_spread_10y3m", {})
                self._send(200, {
                    "status": "ok",
                    "series": len(doc.get("series", {})),
                    "latest": spread.get("as_of"),
                    "headline": st.get("headline"),
                    "signal_active": st.get("signal_active"),
                    "inverted": status.get("inverted"),
                    "errors": len(doc.get("errors", {})),
                    "fetched_at": doc.get("fetched_at"),
                })
            except Exception as exc:  # noqa: BLE001 - absent data IS the unhealthy case
                self._send(503, {"status": "no data", "error": str(exc)})
        elif path == "/api/data":
            self._send(200, build_data_payload(),
                       cache="public, max-age=300, must-revalidate")
        elif path == "/api/status":
            with _lock:
                snapshot = dict(_state)
            snapshot["fred_key"] = bool(FRED_KEY)
            snapshot["econcore"] = econcore.VERSION
            self._send(200, snapshot)
        elif path == "/api/changelog":
            recent, total = econcore.read_revisions(CHANGELOG, CHANGELOG_IN_PAYLOAD)
            self._send(200, {"total": total, "recent": recent})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return


def main():
    if "--refresh" in sys.argv:
        refresh_series()
        return
    if "--once" in sys.argv:
        refresh_series(dry=True)
        return

    print("updater starting: fred_key=%s (schedule: host cron)"
          % bool(FRED_KEY), flush=True)

    def warm():
        try:
            refresh_series()
        except Exception as exc:  # noqa: BLE001 - server must come up regardless
            print("initial fetch failed: %s" % exc, flush=True)

    threading.Thread(target=warm, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
