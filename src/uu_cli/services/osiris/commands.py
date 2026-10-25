"""Osiris course catalogue commands: search, show, filters."""

from __future__ import annotations

import json
import re
import sys
import textwrap

from uu_cli.services.osiris import api


def _filters(args, year: str) -> dict[str, list[str]]:
    """--faculty science etc. as the index's own values; asks Osiris for them only when given."""
    given = {name: getattr(args, name) or [] for name in api.FILTERS}
    if not any(given.values()):
        return {}
    values = api.filter_values(year, args.lang)
    return {name: [api.choose(g, [v for v, _ in values[name]], name) for g in gs]
            for name, gs in given.items() if gs}


def cmd_search(args) -> int:
    if not (args.query or args.teacher or args.open or any(getattr(args, n) for n in api.FILTERS)):
        print("ERROR: Give a course code or name, or a filter (see `uu osiris search --help`).",
              file=sys.stderr)
        return 2
    try:
        year = api.year(args.year, args.lang)
        q = api.query(args.query, year, _filters(args, year), args.teacher, args.open)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    total, courses = api.search(q, size=args.limit, lang=args.lang)
    if args.json:
        print(json.dumps(courses, indent=2, ensure_ascii=False))
    elif not courses:
        print(f"No courses found in {year}.")
    else:
        for c in courses:
            print(_line(c))
    if total > len(courses):
        print(f"[OSIRIS] {len(courses)} of {total} courses in {year}; --limit shows more.",
              file=sys.stderr)
    elif courses and not args.json:
        print(f"[OSIRIS] {total} course{'s' if total != 1 else ''} in {year}.", file=sys.stderr)
    return 0


def _line(c: dict) -> str:
    ec = f"{c['ec']} EC" if c["ec"] else ""
    periods = ", ".join(_short_period(p) for p in c["periods"])
    slots = f"timeslot {'/'.join(_short_slot(t) for t in c['timeslots'])}" if any(
        _short_slot(t) for t in c["timeslots"]) else ""
    level = (c["level"] or "").split(" (")[0] if " (" in (c["level"] or "") else ""
    details = "  ".join(x for x in (ec, f"level {level}" if level else "", periods, slots) if x)
    return f"{c['code']:<12} {c['name']}\n{'':<12} {details}" if details else f"{c['code']:<12} {c['name']}"


def _short_period(p: str) -> str:
    """"Period 1 (07-09-2026 until 13-11-2026)" -> "Period 1"; bare dates stay."""
    return re.sub(r"\s*\(.*\)$", "", p) or p


def _short_slot(t: str) -> str:
    """"C (MON-afternoon, ...)" -> "C"; "not applicable" -> ""."""
    if "applicable" in t.lower() or "niet van toepassing" in t.lower():
        return ""
    return t.split(" (")[0].split(" ")[0]


def cmd_show(args) -> int:
    try:
        year = api.year(args.year, args.lang)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    found = api.find(args.code, year, args.lang)
    if not found:
        print(f"ERROR: No course {args.code} in {year}'s catalogue.", file=sys.stderr)
        return 1
    page = api.course(found["id"], args.lang)
    if args.json:
        print(json.dumps({**found, "sections": page}, indent=2, ensure_ascii=False))
        return 0
    print(f"{found['code']}  {found['name']}  ({found['year']})")
    print(found["url"])
    for s in page:
        print(f"\n== {s['title'] or s['section']} ==")
        _print_fields(s["fields"], "")
    return 0


def _print_fields(fields: list[dict], indent: str) -> None:
    width = 100
    for f in fields:
        label = f"{f['title']}: " if f["title"] else ""
        if "url" in f:
            print(f"{indent}{label}{f['url']}")
        elif "rows" in f:
            if f["title"]:
                print(f"{indent}{f['title']}:")
            for row in f["rows"]:
                if row["title"]:
                    print(f"{indent}  - {row['title']}")
                    _print_fields(row["fields"], indent + "    ")
                else:
                    _print_fields(row["fields"], indent + "  ")
        elif "\n" in f["text"] or len(label + f["text"]) > width - len(indent):
            if label:
                print(f"{indent}{label.rstrip()}")
            for para in f["text"].split("\n"):
                print(textwrap.fill(para, width, initial_indent=indent + "  ",
                                    subsequent_indent=indent + ("    " if para.startswith("• ") else "  "))
                      if para else "")
        else:
            print(f"{indent}{label}{f['text']}")


def cmd_filters(args) -> int:
    try:
        year = api.year(args.year, args.lang)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    values = api.filter_values(year, args.lang)
    if args.json:
        print(json.dumps({k: dict(v) for k, v in values.items()}, indent=2, ensure_ascii=False))
        return 0
    print(f"Filters for {year} (courses in brackets):")
    for name, (_, _, title) in api.FILTERS.items():
        print(f"\n--{name}  ({title})")
        for value, count in sorted(values[name], key=lambda vc: vc[0].lower()):
            print(f"  {value}  [{count}]")
    return 0
