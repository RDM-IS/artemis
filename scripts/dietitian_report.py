#!/usr/bin/env python3.11
"""Render the dietitian report (VA MOVE!) to a PDF — READ-ONLY.

    /usr/bin/python3.11 scripts/dietitian_report.py --fortnight            # 14 days to today
    /usr/bin/python3.11 scripts/dietitian_report.py --week 2026-10-11
    /usr/bin/python3.11 scripts/dietitian_report.py --month 2026-10 --detail summary
    /usr/bin/python3.11 scripts/dietitian_report.py --from 2026-09-22 --to 2026-10-05

`--detail full` (default) is the two-page record; `summary` is page 1 only.
Writes /tmp/dietitian-<start>_<end>-<detail>.pdf (or --out) and prints the
path, the page count and the completeness line. Only SELECTs.

Needs WeasyPrint for the system Python on the box (pango is installed):
    sudo /usr/bin/python3.11 -m pip install weasyprint
`--html` writes the HTML instead, which needs nothing extra.

"Today" is the ACTIVE timezone's local date (quiet_hours.local_today()).
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("dietitian_report.py requires Python 3.11+ (run it with /usr/bin/python3.11).")

import argparse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _d(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--day", type=_d, metavar="YYYY-MM-DD")
    g.add_argument("--week", type=_d, metavar="YYYY-MM-DD", help="7 days ending on this date")
    g.add_argument("--fortnight", nargs="?", const="today", metavar="YYYY-MM-DD",
                   help="14 days ending on this date (default today)")
    g.add_argument("--month", metavar="YYYY-MM")
    g.add_argument("--from", dest="start", type=_d, metavar="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", type=_d, metavar="YYYY-MM-DD")
    ap.add_argument("--detail", choices=("full", "summary"), default="full")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--html", action="store_true", help="write HTML instead of PDF")
    args = ap.parse_args()

    from artemis import dietitian_report as dr
    from artemis.quiet_hours import local_today

    today = local_today()
    if args.day:
        start, end = dr.period("day", args.day)
    elif args.week:
        start, end = dr.period("week", args.week)
    elif args.month:
        y, m = (int(x) for x in args.month.split("-"))
        start, end = dr.period("month", date(y, m, 1))
    elif args.start:
        if not args.end:
            ap.error("--from needs --to")
        start, end = args.start, args.end
    else:
        anchor = today if args.fortnight in (None, "today") else _d(args.fortnight)
        start, end = dr.period("fortnight", anchor)
    if end < start:
        ap.error("the period ends before it starts")

    report = dr.build(dr.collect(start, end))
    doc = dr.render_html(report, args.detail)
    bad = dr.banned_words_in(dr.visible_text(doc))
    if bad:
        # The language rule is a build failure, not a warning.
        print(f"REFUSED: the report text contains judgment words: {bad}", file=sys.stderr)
        return 2

    suffix = "html" if args.html else "pdf"
    out = args.out or Path(f"/tmp/dietitian-{start}_{end}-{args.detail}.{suffix}")
    if args.html:
        out.write_text(doc)
        pages = "n/a (html)"
    else:
        try:
            out.write_bytes(dr.render_pdf(doc))
        except dr.PdfEngineMissing as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        pages = dr.page_count(doc)
    n = (end - start).days + 1
    print(f"{out}\n  period {start} → {end} ({n} days) · pages {pages}\n"
          f"  days with intake recorded: {report.n_recorded} of {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
