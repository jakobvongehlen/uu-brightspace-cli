"""bookings.uu.nl: Springshare LibCal, logged in through LibAuth, SURFconext and UU.

Every booking page wants a login, and the login asks UU for MFA, so UU asks the code even
while its session lasts. The CLI uses the pages you use, as seen on 24-10-2026:

- /reserve/<page> is a search form (category `gid`, type `capacity`, zone, date, from, until,
  and checkboxes for accessible, power and PC); searching is a GET to the same page, which
  lists the free spaces, each with a "Book Now" form.
- "Book Now" posts that form to /space/booking/form, which holds the space for about ten
  minutes and shows the booking form: your name and email, filled in from your login, and a
  terms box. Submitting it (/ajax/equipment/checkout) makes the booking; an error comes back
  as the answer's status, shown in a popup, not on the form.
- The search lists at most 7 "best matches". Every space also has its own page (/seat/<id>, or
  /space/<id> for a room; ?date=YYYY-MM-DD) with an availability grid: clicking a free slot adds
  a pending booking (/spaces/availability/booking/add) whose end you pick from a list, and
  Submit Times (/ajax/space/times) holds it and redirects to the same booking form.
- /ajax/equipment/cart/remove gives a held space back, with the ids on the form's Remove button.
- LibAuth (eu.libauth.com) sits between the pages and SURFconext. Once SURFconext sends it your
  login (/login/saml) it sets SimpleSAMLAuthToken and sends you back to the page. It has been
  seen to stop on that page instead; its new cookie still says the login went through.
- There is no page with your bookings. The confirmation email links to
  /equipment/cancel?id=<code>, which lists that checkout's bookings, each with a Cancel
  button that posts to /ajax/equipment/cancel/<code>/<booking id>. The code is only in the
  email, so `book` keeps its own list (bookings.json) and the email's link fills in the code.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from uu_cli import paths
from uu_cli.login import dump_page

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

# LibAuth's cookie for your login, set anew each time SURFconext sends one.
LIBAUTH_TOKEN = "SimpleSAMLAuthToken"

# The type of space (capacity) each page is for. The search form requires one, and its own
# default ("select a space") finds nothing.
TYPE = {"UBBseats": "-1", "UBUSPseats": "-1", "groupubb": "1", "groupubusp": "1",
        "meetingsubb": "4", "meetingsubusp": "4", "JKH3": "-1", "ash": "-1"}

# The search form's checkboxes (filters[]).
FILTERS = {"accessible": "77", "power": "78", "pc": "693"}

# How long the booking pages may take to answer.
SETTLE_S = 20

# How long submitting the booking form may take to answer.
CHECKOUT_S = 60

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


# A space's own page: its grid has loaded its slots (FullCalendar events).
GRID_READY = "() => getCurrentTimelineInstance(springyPage.locationId).getEvents().length > 0"

# Click the grid's slot that starts at start ("YYYY-MM-DD HH:MM:SS"), as by hand: free, taken or none.
PICK_SLOT = """(start) => {
    const pad = n => String(n).padStart(2, '0');
    const at = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' '
        + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    const grid = getCurrentTimelineInstance(springyPage.locationId);
    const slot = grid.getEvents().find(e => at(e.start) === start);
    if (!slot) return 'none';
    if (slot.extendedProps.status !== 0) return 'taken';
    timeGridClickedOnAvailableRoom(slot, grid);
    return 'free';
}"""

# The space's name in its page's heading, without the location after it.
SPACE_NAME = """() => (document.querySelector('#s-lc-public-header-title')?.firstChild?.textContent
                       || '').trim()"""

# The pending booking that click made, with the ends it may have; null until the site answers.
PENDING = """() => {
    const b = (window.pendingRoomBookings || [])[0];
    return b ? {id: String(b.id), eid: String(b.eid), seat_id: String(b.seat_id || ''),
                end: b.end.format('YYYY-MM-DD HH:mm:ss'), options: b.options || []} : null;
}"""

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
            host = urlparse(page.url).hostname or ""
            if host != HOST and not host.endswith("libauth.com"):  # LibAuth may send us back
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


def libauth_token(ctx: BrowserContext) -> str | None:
    """LibAuth's login cookie, if it has one; a new value means a new login."""
    return next((c["value"] for c in ctx.cookies()
                 if c["name"] == LIBAUTH_TOKEN and c["domain"].endswith("libauth.com")), None)


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

    Holds the space through its "Book Now" form when the search lists it, else (by id) on its
    own page, then submits the booking form with its terms box ticked. When anything stops it
    before the booking is made, the hold is given back.
    """
    defaults = options(ctx, query.place)["defaults"]
    page = open_page(ctx, query.url(defaults))
    match = [r for r in page.evaluate(RESULTS) if r["bookable"] and seat in (r["id"], r["name"])]
    if match:
        chosen = match[0]
        if (chosen["start"][11:16], chosen["end"][11:16]) != (query.start, query.end):
            raise BookingError(f"{chosen['name']} is only free {chosen['start'][11:16]}-"
                               f"{chosen['end'][11:16]} then, not {query.start}-{query.end}")
        field = "seat_id" if chosen["seat_id"] else "eid"
        form = page.locator(f'form[action="/space/booking/form"]:has(input[name="{field}"]'
                            f'[value="{chosen[field]}"])').first
        with page.expect_navigation(wait_until="load", timeout=30_000):
            form.locator("button[type=submit]").click()
    elif seat.isdigit():  # not among the search's best matches
        chosen = _hold_on_own_page(ctx, query, seat)
    else:
        raise BookingError(f"{seat} isn't among the free spaces the search lists then; to book "
                           "one it doesn't list, give its id")
    if not page.locator("#s-lc-eq-bform").count():
        raise BookingError(_alert(page) or f"no booking form came up (at {page.url})")
    session = page.evaluate("() => (window.springyPage || {}).sessionId || ''")
    try:
        page.check("#terms")
        try:
            with page.expect_response(lambda r: "/ajax/equipment/checkout" in r.url,
                                      timeout=CHECKOUT_S * 1000) as answer:
                page.click("#btn-form-submit")
        except PlaywrightTimeout:
            dump_page(page, "debug_booking_no_answer")
            raise BookingError(f"bookings.uu.nl didn't answer the booking within {CHECKOUT_S} s. "
                               "It may still have been made: check your email before booking "
                               "again.") from None
        if not answer.value.ok:
            raise BookingError(_refusal(answer.value))
        status = page.locator(".s-lc-eq-booking-status-msg")
        try:
            status.first.wait_for(timeout=SETTLE_S * 1000)
        except PlaywrightTimeout:
            dump_page(page, "debug_booking_unconfirmed")
            raise BookingError("bookings.uu.nl took the booking but showed no confirmation; "
                               "check your email whether it was made.") from None
        done = page.locator("#s-lc-public-page-content").inner_text().strip()
    except BaseException:
        release(page, chosen["eid"], session)
        raise
    return {**chosen, "status": status.first.inner_text().strip(), "confirmation": done}


def _hold_on_own_page(ctx: BrowserContext, query: Search, space: str) -> dict:
    """Hold space for query's time on its own page, as by hand: its slot, its end, Submit Times.

    Ends on the booking form; returns the space as RESULTS lists one.
    """
    kind = "seat" if TYPE[query.place] == "-1" else "space"
    try:
        page = open_page(ctx, f"{BASE_URL}/{kind}/{space}?date={query.date}")
    except BookingError:  # "Whoops!"
        raise BookingError(f"bookings.uu.nl has no {kind} {space}") from None
    start, end = f"{query.date} {query.start}:00", f"{query.date} {query.end}:00"
    page.wait_for_function(GRID_READY, timeout=SETTLE_S * 1000)
    name = page.evaluate(SPACE_NAME) or space
    if page.evaluate(PICK_SLOT, start) != "free":
        raise BookingError(f"{space} isn't free at {query.start} on {query.date}")
    try:
        page.wait_for_function(PENDING, timeout=SETTLE_S * 1000)
    except PlaywrightTimeout:
        raise BookingError(_alert(page) or f"the site didn't take {space} at {query.start}") from None
    pending = page.evaluate(PENDING)
    try:
        if pending["end"] != end:
            if end not in pending["options"]:
                last = max(pending["options"] or [pending["end"]])[11:16]
                raise BookingError(f"{name} is only free {query.start}-{last} then, "
                                   f"not {query.start}-{query.end}")
            page.select_option(".b-end-date", end)
            page.wait_for_function(f"(end) => (({PENDING})() || {{}}).end === end", arg=end,
                                   timeout=SETTLE_S * 1000)
        if page.is_disabled("#submit_times"):
            raise BookingError(f"the site doesn't take {query.start}-{query.end} for {name}")
        with page.expect_navigation(wait_until="load", timeout=30_000):
            page.click("#submit_times")
    except BaseException:  # the pending booking isn't held yet; drop it as its delete icon does
        page.evaluate("""(id) => typeof removePendingBooking === 'function'
                         && removePendingBooking(id, springyPage.locationId)""", pending["id"])
        raise
    return {"name": name, "details": [], "id": space, "seat_id": pending["seat_id"],
            "eid": pending["eid"], "start": start, "end": end, "bookable": True}


def release(page: Page, eid: str, session) -> None:
    """Give the held spaces back (the booking form's Remove buttons, or eid); best effort."""
    try:  # from the booking page, as its Remove buttons do (the server checks the referrer)
        page.evaluate("""([eid, session]) => {
            const held = [...document.querySelectorAll('.s-lc-eq-remove-single')]
                .map(b => ({eid: String(b.dataset.eid), id: String(b.dataset.id || '0')}));
            return Promise.all((held.length ? held : [{eid, id: '0'}]).map(h =>
                fetch('/ajax/equipment/cart/remove', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'X-Requested-With': 'XMLHttpRequest',
                              'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({...h, session}).toString()})));
        }""", [str(eid), str(session)])
    except PlaywrightError:
        pass  # the hold runs out by itself after about ten minutes


def _refusal(reply) -> str:
    """What the checkout's error answer says (JSON or HTML), as text."""
    try:
        text = reply.text()
    except PlaywrightError:
        text = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            text = str(data.get("error") or data.get("message") or text)
    except ValueError:
        pass
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return text[:300] or f"bookings.uu.nl refused the booking (HTTP {reply.status})"


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
