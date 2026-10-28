"""bookings.uu.nl: Springshare LibCal, logged in through LibAuth, SURFconext and UU.

Every booking page wants a login, and the login asks UU for MFA, so UU asks the code even
while its session lasts. The CLI uses the pages you use, as seen on 24-10-2026:

- /reserve/<page> is a search form (category `gid`, type `capacity`, zone, date, from, until,
  and checkboxes for accessible, power and PC); searching is a GET to the same page, which
  lists the free spaces, each with a "Book Now" form.
- "Book Now" posts that form to /space/booking/form, which holds the space for about ten
  minutes and shows the booking form: your name and email, filled in from your login, and a
  terms box. Submitting it (/ajax/equipment/checkout) makes the booking.
- /ajax/equipment/cart/remove gives a held space back.
- There is no page with your bookings. The confirmation email links to
  /equipment/cancel?id=<code>, which lists that checkout's bookings, each with a Cancel
  button that posts to /ajax/equipment/cancel/<code>/<booking id>. The code is only in the
  email, so `book` keeps its own list (bookings.json) and the email's link fills in the code.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page

from uu_cli import paths

BASE_URL = os.environ.get("UU_BOOKINGS_URL", "https://bookings.uu.nl").rstrip("/")
HOST = urlparse(BASE_URL).hostname

# The booking pages the home page links to: page -> what it is.
PLACES = {
    "UBBseats": "Study spaces, University Library City Centre",
    "UBUSPseats": "Study spaces, University Library Utrecht Science Park",
    "groupubb": "Group spaces, University Library City Centre (2 to 4 hours)",
    "groupubusp": "Group spaces, University Library Utrecht Science Park (2 to 4 hours)",
    "meetingsubb": "Meeting rooms, University Library City Centre (UU staff only)",
    "meetingsubusp": "Meeting rooms, University Library Utrecht Science Park (UU staff only)",
    "JKH3": "Working spaces, Janskerkhof 3 (REBO staff only)",
    "ash": "Working spaces, Adam Smith Hall (REBO staff only)",
}
LOGIN_URL = f"{BASE_URL}/reserve/UBUSPseats"

# The type of space (capacity) each page is for. The search form requires one, and its own
# default ("select a space") finds nothing.
TYPE = {"UBBseats": "-1", "UBUSPseats": "-1", "groupubb": "1", "groupubusp": "1",
        "meetingsubb": "4", "meetingsubusp": "4", "JKH3": "-1", "ash": "-1"}

# The search form's checkboxes (filters[]).
FILTERS = {"accessible": "77", "power": "78", "pc": "693"}

# How long the booking pages may take to answer.
SETTLE_S = 20

# The search form's choices: category, type of space and zone, as [value, label] pairs.
OPTIONS = """() => {
    const opts = sel => [...document.querySelectorAll(sel + ' option')]
        .map(o => [o.value, o.textContent.trim()]);
    const value = name => {
        const el = document.querySelector('#s-lc-search-filters [name="' + name + '"]');
        return el ? el.value : '';
    };
    return {
        category: opts('#s-lc-group'), type: opts('#s-lc-type'), zone: opts('#s-lc-zone'),
        defaults: {gid: value('gid'), capacity: value('capacity'), start: value('start'),
                   end: value('end'), date: value('date')},
    };
}"""

# The search results: one per "Book Now" form.
RESULTS = """() => [...document.querySelectorAll('.s-lc-booking-suggestion')].map(div => {
    const form = div.querySelector('form[action="/space/booking/form"]');
    const field = name => form && form.querySelector('[name="' + name + '"]')?.value || '';
    const details = (div.querySelector('.s-lc-booking-group') || {}).textContent || '';
    return {
        name: (div.querySelector('.s-lc-suggestion-heading') || {}).textContent?.trim() || '',
        details: details.split('|').map(s => s.trim()).filter(Boolean),
        id: field('seat_id') || field('eid'),  // a seat, or a room (which has no seat id)
        seat_id: field('seat_id'), eid: field('eid'), start: field('start'), end: field('end'),
        bookable: !!form,
    };
})"""


# The bookings on a confirmation's cancel page, row by row.
CANCEL_ROWS = """() => [...document.querySelectorAll('.s-lc-cancel-single')].map(btn => {
    const cells = [...btn.closest('tr').querySelectorAll('td')].map(td => td.textContent.trim());
    return {booking_id: String(btn.dataset.id), cells: cells.filter(Boolean)};
})"""


class NotLoggedIn(Exception):
    """bookings.uu.nl sent us away to log in."""


class BookingError(Exception):
    """bookings.uu.nl refused, with its own message where it gave one."""


@dataclass
class Search:
    place: str
    date: str          # YYYY-MM-DD
    start: str         # HH:MM
    end: str           # HH:MM
    gid: str = ""      # category; "" for the page's own default
    capacity: str = ""  # type of space; "" for the page's own default
    zone: str = "0"
    filters: tuple[str, ...] = ()

    def url(self, defaults: dict | None = None) -> str:
        d = defaults or {}
        query = [("m", "t"), ("gid", self.gid or d.get("gid", "0")),
                 ("capacity", self.capacity or TYPE[self.place]), ("zone", self.zone),
                 ("date", self.date), ("date-end", self.date),
                 ("start", self.start), ("end", self.end)]
        query += [("filters[]", FILTERS[f]) for f in self.filters]
        return f"{place_url(self.place)}?{urlencode(query)}"


def place_url(place: str) -> str:
    return f"{BASE_URL}/reserve/{place}"


def open_page(ctx: BrowserContext, url: str) -> Page:
    """Open one of the booking pages; raises NotLoggedIn when it sends us to log in."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="load", timeout=30_000)
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline:
        try:
            if urlparse(page.url).hostname != HOST:
                raise NotLoggedIn(url)
            if page.locator(".s-lc-libauth-logout").count():
                return page
            if page.title().startswith("Whoops"):
                raise BookingError(f"{url} is broken on the site itself (\"Whoops!\")")
        except PlaywrightError:
            pass  # navigating; look again
        page.wait_for_timeout(250)
    raise NotLoggedIn(url)


def logged_in(ctx: BrowserContext) -> bool:
    try:
        open_page(ctx, LOGIN_URL)
        return True
    except NotLoggedIn:
        return False


def options(ctx: BrowserContext, place: str) -> dict:
    """The page's search choices (category, type, zone) and its defaults."""
    return open_page(ctx, place_url(place)).evaluate(OPTIONS)


def search(ctx: BrowserContext, query: Search) -> list[dict]:
    """The free spaces for query, best matches first, as the page lists them."""
    defaults = options(ctx, query.place)["defaults"]
    page = open_page(ctx, query.url(defaults))
    if not page.locator("#s-lc-eq-search-results").count():
        raise BookingError(_alert(page) or "the page showed no search results")
    return [r for r in page.evaluate(RESULTS) if r["bookable"]]


def book(ctx: BrowserContext, query: Search, seat: str) -> dict:
    """Book the space named seat (its id or name) for query's time; returns what was booked.

    Holds the space through its "Book Now" form, then submits the booking form with its terms
    box ticked. When anything stops it before the booking is made, the hold is given back.
    """
    defaults = options(ctx, query.place)["defaults"]
    page = open_page(ctx, query.url(defaults))
    results = page.evaluate(RESULTS)
    match = [r for r in results if r["bookable"] and seat in (r["id"], r["name"])]
    if not match:
        raise BookingError(f"{seat} isn't free then; `uu bookings search` lists what is")
    chosen = match[0]
    if (chosen["start"][11:16], chosen["end"][11:16]) != (query.start, query.end):
        raise BookingError(f"{chosen['name']} is only free {chosen['start'][11:16]}-"
                           f"{chosen['end'][11:16]} then, not {query.start}-{query.end}")
    field = "seat_id" if chosen["seat_id"] else "eid"
    form = page.locator(f'form[action="/space/booking/form"]:has(input[name="{field}"]'
                        f'[value="{chosen[field]}"])').first
    with page.expect_navigation(wait_until="load", timeout=30_000):
        form.locator("button[type=submit]").click()
    if not page.locator("#s-lc-eq-bform").count():
        raise BookingError(_alert(page) or f"no booking form came up (at {page.url})")
    session = page.evaluate("() => (window.springyPage || {}).sessionId || ''")
    try:
        page.check("#terms")
        page.click("#btn-form-submit")
        page.wait_for_selector(".s-lc-eq-booking-status-msg, .alert-danger, #submit-errors:visible",
                               timeout=SETTLE_S * 1000)
        status = page.locator(".s-lc-eq-booking-status-msg")
        if not status.count():
            raise BookingError(_alert(page) or "the booking form didn't confirm the booking")
        done = page.locator("#s-lc-public-page-content").inner_text().strip()
    except BaseException:
        release(page, chosen["eid"], session)
        raise
    return {**chosen, "status": status.first.inner_text().strip(), "confirmation": done}


def release(page: Page, eid: str, session) -> None:
    """Give a held space back (the booking form's Remove button); best effort."""
    try:  # from the booking page, as its Remove button does (the server checks the referrer)
        page.evaluate("""([eid, session]) => fetch('/ajax/equipment/cart/remove', {
            method: 'POST', credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest',
                      'Content-Type': 'application/x-www-form-urlencoded'},
            body: new URLSearchParams({eid, id: '0', session}).toString()})""",
                      [str(eid), str(session)])
    except PlaywrightError:
        pass  # the hold runs out by itself after about ten minutes


def _alert(page: Page) -> str:
    """The page's own error message, if it shows one."""
    for sel in ("#submit-errors", ".alert-danger", ".alert-warning"):
        el = page.locator(sel)
        if el.count() and el.first.is_visible():
            return el.first.inner_text().strip()
    return ""


# ── your bookings: the confirmation's cancel page, and the CLI's own list ─────

def confirmation_code(link: str) -> str:
    """The code in the confirmation email's cancel link (or the code itself)."""
    found = re.search(r"[?&]id=([0-9a-zA-Z]+)", link) or re.fullmatch(r"([0-9a-zA-Z]+)", link.strip())
    if not found:
        raise ValueError(f"{link} isn't a cancel link from the confirmation email "
                         f"({BASE_URL}/equipment/cancel?id=...)")
    return found.group(1)


def on_confirmation(ctx: BrowserContext, code: str) -> tuple[Page, list[dict]]:
    """The cancel page of one confirmation, and the bookings it still lists."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"{BASE_URL}/equipment/cancel?id={code}", wait_until="load", timeout=30_000)
    if urlparse(page.url).hostname != HOST or page.title().startswith(("Whoops", "404")) \
            or "/status" in page.url:
        raise BookingError(f"bookings.uu.nl doesn't know confirmation {code}")
    rows = []
    for row in page.evaluate(CANCEL_ROWS):
        cells = row["cells"]  # item, category, from, to, the button's own text
        rows.append({"booking_id": row["booking_id"], "name": cells[0] if cells else "",
                     "category": cells[1] if len(cells) > 1 else "",
                     "from": cells[2] if len(cells) > 2 else "", "to": cells[3] if len(cells) > 3 else ""})
    return page, rows


def cancel(ctx: BrowserContext, code: str, booking_id: str | None = None) -> list[dict]:
    """Cancel one booking of a confirmation, or all it lists; returns what was cancelled."""
    page, rows = on_confirmation(ctx, code)
    chosen = [r for r in rows if booking_id in (None, r["booking_id"])]
    if not chosen:
        raise BookingError(f"confirmation {code} lists no booking {booking_id}" if booking_id
                           else f"confirmation {code} has nothing left to cancel")
    for row in chosen:  # from the cancel page, as its Cancel Booking button does
        answer = page.evaluate("""async (url) => {
            const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
                                        headers: {'X-Requested-With': 'XMLHttpRequest'}});
            try { return await r.json(); } catch (e) { return {success: false, error: 'HTTP ' + r.status}; }
        }""", f"/ajax/equipment/cancel/{code}/{row['booking_id']}")
        if not answer.get("success"):
            raise BookingError(f"{row['name']}: {answer.get('error') or 'not cancelled'}")
    return chosen


def bookings_file():
    return paths.TOKEN_FILE.parent / "bookings.json"


def saved_bookings() -> list[dict]:
    try:
        return json.loads(bookings_file().read_text())
    except (OSError, ValueError):
        return []


def save_bookings(rows: list[dict]) -> None:
    paths.write_private(bookings_file(), json.dumps(rows, indent=1, ensure_ascii=False))


def remember(entry: dict) -> None:
    """Add a booking `book` made to the CLI's own list."""
    save_bookings([*saved_bookings(), entry])


def attach_code(code: str, rows: list[dict], cancelled: bool = False) -> None:
    """Note a confirmation's code (and whether it was cancelled) on the bookings it lists."""
    saved = saved_bookings()
    for row in rows:
        for entry in saved:
            if entry.get("name") == row["name"] and entry.get("code") in (None, code) \
                    and _same_start(entry, row):
                entry.update(code=code, booking_id=row["booking_id"])
                if cancelled:
                    entry["cancelled"] = True
                break
        else:
            saved.append({"name": row["name"], "code": code, "booking_id": row["booking_id"],
                          "from": row["from"], "to": row["to"], **({"cancelled": True} if cancelled else {})})
    save_bookings(saved)


def _same_start(entry: dict, row: dict) -> bool:
    """Whether a saved booking starts when a cancel page's row does ("10:00 Sunday, October 25, 2026")."""
    if "start" not in entry:
        return entry.get("from") == row["from"]
    start = time.strptime(entry["start"], "%Y-%m-%d %H:%M:%S")
    return row["from"].startswith(time.strftime("%H:%M", start)) and \
        time.strftime("%B %-d, %Y", start) in row["from"]
