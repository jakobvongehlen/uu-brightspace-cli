"""bookings.uu.nl commands: login, places, options, search, book, list, show, cancel."""

from __future__ import annotations

import json
import re
import sys
import time

from playwright.sync_api import BrowserContext

from uu_cli import auth
from uu_cli.login import log_in, single_sign_on
from uu_cli.services.bookings import api

TERMS = ("Booking means agreeing to the page's Terms & Conditions: the library's opening hours "
         "and house rules (https://www.uu.nl/en/university-library/practical-information). "
         "Read them, then add --accept-terms.")


def ensure_login(ctx: BrowserContext, args) -> bool:
    """Log in unless already in; UU asks a code here even while its session lasts (MFA)."""
    if api.logged_in(ctx):
        return True
    print("[AUTH] Logging in to bookings.uu.nl...", file=sys.stderr)
    if single_sign_on(ctx, api.LOGIN_URL) and api.logged_in(ctx):
        print("[AUTH] ✓ Logged in with your existing UU session.", file=sys.stderr)
        return True
    if not auth.ready_to_login(args):
        print(f"ERROR: Cannot log in: {auth.CANNOT_LOGIN}.", file=sys.stderr)
        return False
    log_in(ctx, api.LOGIN_URL, args, name="bookings.uu.nl")
    return api.logged_in(ctx)


def cmd_login(ctx: BrowserContext, args) -> int:
    if not ensure_login(ctx, args):
        return 1
    print("Logged in to bookings.uu.nl.", file=sys.stderr)
    return 0


def cmd_places(ctx: BrowserContext | None, args) -> int:
    for place, what in api.PLACES.items():
        print(f"{place:<14} {what}")
    return 0


def cmd_options(ctx: BrowserContext, args) -> int:
    if not ensure_login(ctx, args):
        return 1
    opts = api.options(ctx, args.place)
    if args.json:
        print(json.dumps(opts, indent=2, ensure_ascii=False))
        return 0
    print(f"{args.place}: {api.PLACES[args.place]}")
    for key, flag in (("category", "--category"), ("type", "--type"), ("zone", "--zone")):
        print(f"\n{flag}:")
        for value, label in opts[key]:
            if value not in ("0", ""):
                print(f"  {value:>6}  {label}")
    print(f"\nOther filters: {', '.join('--' + f for f in api.FILTERS)}")
    return 0


def _choice(given: str | None, choices: list, flag: str) -> str:
    """A choice by its number or (part of) its label; "" when not given."""
    if not given:
        return ""
    for value, label in choices:
        if given == value:
            return value
    found = [(v, label) for v, label in choices if given.lower() in label.lower() and v != "0"]
    if len(found) == 1:
        return found[0][0]
    names = ", ".join(f"{v} ({label})" for v, label in choices if v != "0")
    what = "matches several" if found else "matches none"
    raise ValueError(f"{flag} {given} {what} of: {names}")


def _query(ctx: BrowserContext, args) -> api.Search:
    opts = api.options(ctx, args.place)
    d = opts["defaults"]
    for name, value in (("--date", args.date), ("--from", args.start), ("--until", args.end)):
        pattern = r"\d{4}-\d{2}-\d{2}" if name == "--date" else r"\d{1,2}:\d{2}"
        if value and not re.fullmatch(pattern, value):
            raise ValueError(f"{name} {value}: write it as " + ("2026-10-25" if name == "--date" else "14:00"))
    return api.Search(
        place=args.place, date=args.date or d["date"],
        start=(args.start or d["start"]).zfill(5), end=(args.end or d["end"]).zfill(5),
        gid=_choice(args.category, opts["category"], "--category"),
        capacity=_choice(args.type, opts["type"], "--type"),
        zone=_choice(args.zone, opts["zone"], "--zone") or "0",
        filters=tuple(f for f in api.FILTERS if getattr(args, f)))


def cmd_search(ctx: BrowserContext, args) -> int:
    if not ensure_login(ctx, args):
        return 1
    try:
        query = _query(ctx, args)
        results = api.search(ctx, query)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    except api.BookingError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    print(f"[BOOKINGS] {query.date} {query.start}-{query.end}", file=sys.stderr)
    if not results:
        print("Nothing free then.")
        return 0
    for r in results:
        when = f"{r['start'][11:16]}-{r['end'][11:16]}"
        note = "" if when == f"{query.start}-{query.end}" else f"  (free {when})"
        print(f"{r['id']:>7}  {r['name']:<22} {' | '.join(r['details'])}{note}")
    return 0


def cmd_book(ctx: BrowserContext, args) -> int:
    if not args.accept_terms:
        print(f"ERROR: {TERMS}", file=sys.stderr)
        return 2
    if not ensure_login(ctx, args):
        return 1
    try:
        query = _query(ctx, args)
        booked = api.book(ctx, query, args.space)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    except api.BookingError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    api.remember({"place": args.place, "name": booked["name"], "id": booked["id"],
                  "start": booked["start"], "end": booked["end"],
                  "booked_at": _now()})
    print(f"Booked {booked['name']}, {query.date} {query.start}-{query.end}.")
    print(f"[BOOKINGS] {booked['status']} To cancel it: `uu bookings cancel <the email's cancel "
          "link>`.", file=sys.stderr)
    return 0


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def cmd_list(ctx: BrowserContext | None, args) -> int:
    """The bookings this CLI made (bookings.uu.nl has no page that lists yours)."""
    now = _now()
    rows = [b for b in api.saved_bookings()
            if args.all or (not b.get("cancelled") and b.get("end", "9999") >= now)]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No upcoming bookings made with `uu bookings book`.")
        return 0
    for b in rows:
        when = (f"{b['start'][:16]}-{b['end'][11:16]}" if "start" in b else f"{b.get('from', '?')}")
        state = "  (cancelled)" if b.get("cancelled") else ""
        code = f"  confirmation {b['code']}" if b.get("code") else ""
        print(f"{when}  {b['name']}{code}{state}")
    print("[BOOKINGS] Only bookings made here, or looked up with `show`: the site lists none. "
          "The cancel link is in each confirmation email.", file=sys.stderr)
    return 0


def cmd_show(ctx: BrowserContext, args) -> int:
    try:
        code = api.confirmation_code(args.link)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    if not ensure_login(ctx, args):
        return 1
    try:
        _, rows = api.on_confirmation(ctx, code)
    except api.BookingError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    api.attach_code(code, rows)
    if not rows:
        print("Nothing left on this confirmation: cancelled, or already past.")
    for r in rows:
        print(f"{r['booking_id']:>9}  {r['name']:<22} {r['from']} - {r['to']}")
    return 0


def cmd_cancel(ctx: BrowserContext, args) -> int:
    try:
        code = api.confirmation_code(args.link)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    if not ensure_login(ctx, args):
        return 1
    try:
        cancelled = api.cancel(ctx, code, args.booking)
    except api.BookingError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    api.attach_code(code, cancelled, cancelled=True)
    for r in cancelled:
        print(f"Cancelled {r['name']}, {r['from']} - {r['to']}.")
    return 0
