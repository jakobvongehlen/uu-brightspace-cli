"""remindo.uu.nl's own pages, logged in with your Solis-id.

Remindo (Paragin) renders most pages on the server; the CLI reads them as you see them in a
browser. As seen on 29-10-2026:

- Login: /login links to /sso/auth/320-solisid_universiteit_utrecht, which goes through
  Paragin's broker remindoconnect.com to UU's Solis-id page (no SURFconext), and back.
  Logged out, every page sends you to /login.
- /dashboard: the agenda (upcoming exams and practices) and a test overview per course.
- /results/list/?tab=<practice|graded practice|exam>: your attempts, a table per tab, with a
  "Result details" link (/results/view/<id>) where you may view the result.
- /results/view/<id>: a Vue page whose data sits as JSON in a `properties` attribute (course,
  test, times, each question's status); each question comes from /api/v2/itemresult/<id>/block
  as QTI HTML, your answer marked qti-candidateResponse and the correct one qti-correctResponse.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page

BASE_URL = os.environ.get("UU_REMINDO_URL", "https://remindo.uu.nl").rstrip("/")
SSO_URL = f"{BASE_URL}/sso/auth/320-solisid_universiteit_utrecht"
HOST = urlparse(BASE_URL).hostname

# How long a page may take to settle: its own content, or a redirect to /login.
SETTLE_S = 15

# Where a question's images live: Remindo itself, or Paragin's test manager behind it.
IMAGE_HOST_SUFFIX = ".remindotoetsmanager.nl"

# The tabs of /results/list/, by the name `results --type` takes.
RESULT_TABS = {"exam": "exam", "graded": "graded practice", "practice": "practice"}

# A page's main text and its links, as you'd read and click them.
CONTENT = """() => {
    const main = document.querySelector('#content') || document.body;
    const links = [...main.querySelectorAll('a[href]')]
        .map(a => ({text: a.innerText.trim().replace(/\\s+/g, ' '), href: a.href}))
        .filter(l => l.text && !l.href.startsWith('javascript:') && !l.href.includes('#'));
    return {title: document.title, text: main.innerText, links};
}"""

# The dashboard's agenda: one entry per exam or practice, under its month.
AGENDA = """() => {
    const clean = el => el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
    const out = [];
    for (const li of document.querySelectorAll('ul.agenda li.month')) {
        const month = clean(li.querySelector('h3'));
        let day = '';
        for (const tr of li.querySelectorAll('tr')) {
            const th = tr.querySelector('th');
            if (th) {  // rows of the same day share the first row's header; a period has two dates
                const dates = [...th.querySelectorAll('.period_title, .day_title')].map(clean);
                day = dates.length ? dates.join(' – ') : clean(th);
            }
            const td = tr.querySelector('td.moment');
            if (!td) continue;
            const span = td.querySelector('.timespan');
            const start = td.querySelector('a[href*="/moment/start/"]');
            out.push({
                month, day,
                title: clean(td.querySelector('h4')),
                type: clean(td.querySelector('.recipetype')),
                time: span ? span.innerHTML.split(/<br\\s*\\/?>/i)
                    .map(s => s.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').trim())
                    .filter(Boolean).join(' – ') : '',
                location: clean(td.querySelector('.location')),
                start: start ? start.href.split("?")[0] : null,
            });
        }
    }
    return out;
}"""

# One tab of /results/list/: its rows, and the "Result 1 - n of m" label.
RESULTS = """() => {
    const clean = el => el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
    const rows = [...document.querySelectorAll('tr[id^="result_"]')].map(tr => {
        const td = tr.querySelectorAll('td');  // course, test, start, progress, result, status, icons
        const view = tr.querySelector('a[href*="/results/view/"]');
        const bar = td[3] && td[3].querySelector('[aria-label]');
        return {
            id: tr.id.replace('result_', ''),
            course: clean(td[0]), test: clean(td[1]), start: clean(td[2]),
            progress: bar ? bar.getAttribute('aria-label') : '',
            result: clean(td[4]).replace(/^–$/, ''), status: clean(td[5]),
            viewable: !!view,
        };
    });
    return {rows, label: clean(document.querySelector('.tabletabs .label'))};
}"""

# A question's blocks (QTI HTML) as text, choices and pairs, your answer and the correct one.
QUESTION = """blocks => {
    const parse = h => new DOMParser().parseFromString(h || '', 'text/html').body;
    const text = node => {
        const el = node.cloneNode(true);
        // A fill-in field inside the text: your answer, and the correct one when it differs.
        el.querySelectorAll('.qti-inlineInteraction').forEach(i => {
            const value = cls => {
                const e = i.querySelector('.' + cls);
                if (!e) return '';
                const c = e.cloneNode(true);
                c.querySelectorAll('.qti-points').forEach(p => p.remove());
                return c.textContent.replace(/\\s+/g, ' ').trim();
            };
            const yours = value('qti-candidateResponse'), correct = value('qti-correctResponse');
            const points = ((i.querySelector('.qti-points') || {}).textContent || '').trim();
            i.replaceWith(` [your answer: ${yours || '–'}` + (!correct ? '' : yours === correct ? ' ✓'
                : ` | correct: ${correct}`) + (points ? ` (${points})` : '') + '] ');
        });
        el.querySelectorAll('.qti-points, .qti-choice-radio, script, style').forEach(e => e.remove());
        el.querySelectorAll('img').forEach(i => i.replaceWith(` [image: ${i.getAttribute('src')}] `));
        el.querySelectorAll('br').forEach(b => b.replaceWith('\\n'));
        el.querySelectorAll('td, th').forEach(e => e.append(' | '));
        el.querySelectorAll('p, div, li, tr, h1, h2, h3, h4, h5, table, span.qtistyle-paragraph')
            .forEach(e => e.append('\\n'));
        return el.textContent.split('\\n')
            .map(l => l.replace(/[ \\t\\u00a0]+/g, ' ').replace(/^\\s*\\|\\s*|\\s*\\|\\s*$/g, '').trim())
            .filter(Boolean).join('\\n');
    };
    const marks = el => ({yours: el.classList.contains('qti-candidateResponse'),
                          correct: el.classList.contains('qti-correctResponse')});
    return blocks.map(b => {
        const body = parse(b.html);
        const prompt = text(parse(b.prompt));
        const kind = (b.class || '').split('\\\\').pop();
        if (b.type !== 'interaction') return {kind: 'text', text: text(body)};
        if (kind === 'ChoiceInteraction') {
            return {kind: 'choice', prompt, choices: [...body.querySelectorAll('.qti-simpleChoice-block')]
                .map(li => ({text: text(li), points: (li.querySelector('.qti-points') || {}).textContent || '',
                             ...marks(li)}))};
        }
        if (kind === 'MatchInteraction') {
            const [head, ...rows] = body.querySelectorAll('table.qti-match-table tr');
            const targets = head ? [...head.querySelectorAll('.qti-simpleAssociableChoice-target')].map(text) : [];
            return {kind: 'match', prompt, pairs: rows.map(tr => {
                const cells = [...tr.querySelectorAll('.qti-simpleAssociableChoice-choice')];
                const pick = cls => cells.flatMap((c, i) => c.classList.contains(cls) ? [targets[i]] : []);
                return {source: text(tr.querySelector('.qti-simpleAssociableChoice-source') || tr),
                        yours: pick('qti-candidateResponse'), correct: pick('qti-correctResponse')};
            })};
        }
        // Another kind of question: its text, and whatever it marks as yours or correct.
        const marked = cls => [...body.querySelectorAll('.' + cls)].map(text).filter(Boolean);
        return {kind: kind || 'other', prompt, text: text(body),
                yours: marked('qti-candidateResponse'), correct: marked('qti-correctResponse')};
    });
}"""


class NotLoggedIn(Exception):
    """Remindo sent us to its login page."""


class RemindoError(Exception):
    """Remindo didn't give what was asked, with why."""


def _url(path: str) -> str:
    return urljoin(BASE_URL + "/", path.lstrip("/"))


def _on_login_page(url: str) -> bool:
    u = urlparse(url)
    return u.hostname != HOST or u.path.rstrip("/") == "/login"


def open_page(ctx: BrowserContext, path: str = "/") -> Page:
    """Open one of Remindo's pages; raises NotLoggedIn when Remindo wants a login first."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(_url(path), wait_until="load", timeout=30_000)
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline:
        try:
            if _on_login_page(page.url):
                raise NotLoggedIn(path)
            if page.locator("#content").count():
                return page
        except PlaywrightError:
            pass  # navigating; look again
        page.wait_for_timeout(250)
    if _on_login_page(page.url):
        raise NotLoggedIn(path)
    return page


def logged_in(ctx: BrowserContext) -> bool:
    try:
        open_page(ctx)
        return True
    except NotLoggedIn:
        return False


def content(ctx: BrowserContext, path: str = "/") -> dict:
    """A page's title, main text and links (absolute)."""
    page = open_page(ctx, path)
    return {"url": page.url, **page.evaluate(CONTENT)}


def agenda(ctx: BrowserContext) -> list[dict]:
    """The dashboard's upcoming exams and practices, in its order (by date)."""
    return open_page(ctx, "/dashboard?order=date").evaluate(AGENDA)


def results(ctx: BrowserContext, tab: str) -> tuple[list[dict], str]:
    """Your attempts on one tab of the results list (newest first), and its "Result 1 - n of m" label."""
    got = open_page(ctx, f"/results/list/?tab={RESULT_TABS[tab].replace(' ', '+')}").evaluate(RESULTS)
    return got["rows"], got["label"]


# A same-origin fetch from one of Remindo's pages: the page's cookies, and Playwright's routes.
FETCH = """async url => {
    const r = await fetch(url, {credentials: 'same-origin'});
    return {url: r.url, status: r.status, text: await r.text()};
}"""


def _get(ctx: BrowserContext, path: str) -> str:
    """A page or API answer with the session, without rendering it; NotLoggedIn when sent to /login."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if urlparse(page.url).hostname != HOST:
        page = open_page(ctx, "/dashboard")
    r = page.evaluate(FETCH, _url(path))
    if _on_login_page(r["url"]):
        raise NotLoggedIn(path)
    if r["status"] >= 400:
        raise RemindoError(f"{path}: HTTP {r['status']}")
    return r["text"]


def result(ctx: BrowserContext, result_id: str) -> dict:
    """One result: course, test, type, times, status, counts and each question's status."""
    page = _get(ctx, f"/results/view/{result_id}")
    m = re.search(r"""\sproperties=(["'])(\{.*?)\1""", page, re.S)
    if not m:
        raise RemindoError(f"Remindo shows no result {result_id}; viewing it may be disabled "
                           f"(see `uu remindo results`).")
    props = json.loads(html.unescape(m.group(2)))
    menu = props.get("menuOptions") or {}
    res = menu.get("result") or {}
    items = sorted((props.get("itemResults") or {}).values(), key=lambda i: i.get("questionNumber") or 0)
    return {
        "id": str(result_id),
        "course": (menu.get("study") or {}).get("name", ""),
        "test": (menu.get("recipe") or {}).get("name", ""),
        "type": (menu.get("recipe") or {}).get("type", ""),
        "start": res.get("startTime", ""),
        "end": res.get("endTime", ""),
        "status": res.get("status", ""),
        "score": res.get("score"),           # only where the test shows it
        "max_score": res.get("maximumScore"),
        # The page's own counts; the result's copy can say all correct when it isn't (a practice).
        "counts": {k: v for k, v in (props.get("progress") or res.get("progressBarValues") or {}).items() if v},
        "grade": res.get("gradeValue"),      # a practice's percentage, where shown
        "questions": [{"number": i.get("questionNumber"), "id": str(i["id"]), "status": i.get("status", "")}
                      for i in items],
    }


def question(ctx: BrowserContext, item_id: str) -> list[dict]:
    """One question of a result, block by block (see QUESTION)."""
    data = json.loads(_get(ctx, f"/api/v2/itemresult/{item_id}/block"))
    if not data.get("success"):
        raise RemindoError(f"Remindo didn't give question {item_id}.")
    blocks = ((data.get("data") or {}).get("content") or {}).get("blocks") or []
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return page.evaluate(QUESTION, blocks)


def image(ctx: BrowserContext, url: str) -> bytes:
    """One of a question's images, fetched as the browser would show it (cross-origin, so as a page)."""
    host = urlparse(url).hostname or ""
    if host != HOST and not host.endswith(IMAGE_HOST_SUFFIX):
        raise RemindoError(f"not one of Remindo's images: {url}")
    page = ctx.new_page()
    try:
        resp = page.goto(url, timeout=30_000)
        if not resp or resp.status >= 400:
            raise RemindoError(f"{url}: HTTP {resp.status if resp else '?'}")
        return resp.body()
    finally:
        page.close()
