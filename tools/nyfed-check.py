#!/usr/bin/env python3
"""Crosscheck our monthly spread against the NY Fed's, and read their latest
recession probability for the monthly meta.json ritual.

Host-side build/maintenance tool, NOT part of the runtime: the workbook is
legacy .xls, which needs xlrd, and the containers stay stdlib-only. Run it
from any venv with xlrd installed:

    python3 -m venv /tmp/nyfed-venv && /tmp/nyfed-venv/bin/pip -q install xlrd
    /tmp/nyfed-venv/bin/python tools/nyfed-check.py

What it does:
  1. downloads allmonth.xls (or reads --xls PATH);
  2. prints the last published spread month and the latest three probability
     rows, which is everything needed to update meta.json by hand;
  3. if data/series.json exists, diffs our us_spread_10y3m_monthly against
     their Spread column over the overlap and reports the worst absolute
     difference per decade. Exits 1 if the overall worst exceeds 0.01, which
     would mean the constructions have diverged.
"""

import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime

URL = ("https://www.newyorkfed.org/medialibrary/media/research/"
       "capital_markets/allmonth.xls")
HERE = os.path.dirname(os.path.abspath(__file__))
SERIES = os.path.join(HERE, "..", "data", "series.json")

try:
    import xlrd
except ImportError:
    sys.exit("xlrd is required: pip install xlrd (see the module docstring)")


def arg(flag, default=None):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main():
    path = arg("--xls")
    if not path:
        fh = tempfile.NamedTemporaryFile(suffix=".xls", delete=False)
        req = urllib.request.Request(URL, headers={"User-Agent": "yield.chrislawrence.ca build check"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            fh.write(resp.read())
        fh.close()
        path = fh.name
        print("downloaded %s (%d bytes)" % (URL.rsplit("/", 1)[1], os.path.getsize(path)))

    sheet = xlrd.open_workbook(path).sheets()[0]

    def month(row):
        return datetime.strptime(sheet.cell_value(row, 0), "%d-%b-%Y").strftime("%Y-%m")

    spread_rows = [r for r in range(1, sheet.nrows) if sheet.cell_type(r, 4) == 2]
    prob_rows = [r for r in range(1, sheet.nrows) if sheet.cell_type(r, 5) == 2]
    last = spread_rows[-1]
    print("\ntheir last data month: %s  spread=%+.4f  (10y %.2f, 3m BEY %.4f)"
          % (month(last), sheet.cell_value(last, 4),
             sheet.cell_value(last, 1), sheet.cell_value(last, 3)))
    print("latest probabilities (value is for the 12 months THROUGH that month):")
    for row in prob_rows[-3:]:
        print("  %s  %.1f%%" % (month(row), sheet.cell_value(row, 5) * 100))
    print("meta.json wants: value=%.1f, as_of=%s, horizon=%s, last_reviewed=today"
          % (sheet.cell_value(prob_rows[-1], 5) * 100, month(last), month(prob_rows[-1])))

    series_path = arg("--series", SERIES)
    if not os.path.exists(series_path):
        print("\nno %s; skipping the spread crosscheck" % series_path)
        return

    with open(series_path) as fh:
        ours_entry = json.load(fh)["series"].get("us_spread_10y3m_monthly")
    if not ours_entry:
        print("\nus_spread_10y3m_monthly missing from series.json; nothing to check")
        return
    ours = {date[:7]: value for date, value in ours_entry["obs"]}

    worst = {}
    for row in spread_rows:
        ym = month(row)
        if ym not in ours:
            continue
        diff = abs(ours[ym] - sheet.cell_value(row, 4))
        decade = ym[:3] + "0s"
        if diff > worst.get(decade, (0, ""))[0]:
            worst[decade] = (diff, ym)
    print("\nworst absolute difference vs their Spread column, by decade:")
    overall = 0.0
    for decade in sorted(worst):
        diff, ym = worst[decade]
        overall = max(overall, diff)
        print("  %s  %.6f (%s)" % (decade, diff, ym))
    print("overall worst: %.6f over %d overlapping months"
          % (overall, len([r for r in spread_rows if month(r) in ours])))
    if overall > 0.01:
        sys.exit("DIVERGED: constructions differ by more than a basis point")
    print("constructions match.")


if __name__ == "__main__":
    main()
