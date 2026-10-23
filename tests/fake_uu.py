"""An offline stand-in for UU's Solis-id login, Brightspace, Konjoin, the library proxy, UU's
GitLab, edu.nl and SURFfilesender's login, served through Playwright routing.

It models what the CLI relies on, not the real services: a NetIQ-style form with a one-time-code
step and a single-sign-on session, SAML-style hops back that set a session cookie, a D2L token
in localStorage, SURFconext's institution picker in front of Konjoin, edu.nl and
SURFfilesender, the library proxy's login link in front of an article with a PDF, GitLab's own
Solis-ID sign-in with CSRF-checked API calls, edu.nl's shortening form and overview,
SURFfilesender's profile page and client configuration, and the API endpoints the commands
call. Every other request is aborted and redirects happen in the page, so nothing leaves the
machine. (SURFfilesender's REST API isn't browser traffic: tests/test_filesender.py fakes it.)
"""

from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pyotp

BRIGHTSPACE = "https://uu.brightspace.com"
KONJOIN = "https://uu.konjoin.nl"
SURFCONEXT = "https://engine.surfconext.nl"
LIBRARY = "https://utrechtuniversity.idm.oclc.org"
GITLAB = "https://git.science.uu.nl"
EDUNL = "https://edu.nl"
FILESENDER = "https://filesender.surf.nl"
IDP = "https://login.uu.nl"
SOLISID = "1234567"
PASSWORD = "correct horse"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
ORG_UNIT = "4242"
COURSE = "/content/enforced/4242-INFOMAI"
PDF = b"%PDF-1.4\n% fake course file\n"

# Where UU's login sends you back to, per service (the "sp" parameter).
ACS = {
    "brightspace": f"{BRIGHTSPACE}/d2l/lp/auth/saml/acs",
    "konjoin": f"{KONJOIN}/saml2/acs",
    "library": f"{LIBRARY}/Shibboleth.sso/SAML2/POST",
    "edunl": f"{EDUNL}/mellon/postResponse",
    "filesender": f"{FILESENDER}/simplesaml/module.php/saml/sp/saml2-acs.php/default-sp",
}

# The services that reach UU's login through SURFconext's institution picker.
VIA_SURFCONEXT = ("konjoin", "edunl", "filesender")

# Lets the CLI's fetch() from about:blank reach the API, as a cached-token command does.
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Accept",
}

LOGIN_FORM = """
<form method="post" action="/nidp/app/login?sid=0&sp={sp}">
  <input id="Ecom_User_ID" name="Ecom_User_ID" type="text">
  <input id="Ecom_Password" name="Ecom_Password" type="password">
  <button id="loginButton2" type="submit">Login</button>
</form>"""

OTP_FORM = """
<form method="post" action="/nidp/app/login?sid=1&sp={sp}">
  <label for="nffc">Code from your authenticator app</label>
  <input id="nffc" name="nffc" type="text" autocomplete="one-time-code" maxlength="6">
  <button id="loginButton2" type="submit">Verify</button>
</form>"""

WAYF = f"""<h1>Select an institution to log in</h1>
<ul>
  <li><a href="{SURFCONEXT}/wayf/umc">UMC Utrecht</a></li>
  <li><a href="{IDP}/nidp/app/login?sp={{sp}}">Utrecht University</a></li>
</ul>"""

HOME = """<h1>My Home</h1>
<script>localStorage.setItem('D2L.Fetch.Tokens',
  JSON.stringify({"*:*:*": {"access_token": "TOKEN", "expires_at": 0}}));</script>"""

READING = """<h1>Reading</h1>
<p>See the <a href="handout.pdf">handout</a> and <a href="https://example.org/x.pdf">an outside link</a>.</p>"""

API = {
    "/d2l/api/lp/1.43/enrollments/myenrollments/": {"Items": [
        {"OrgUnit": {"Id": 4242, "Code": "INFOMAI", "Name": "Fake Course", "Type": {"Id": 3}}},
        {"OrgUnit": {"Id": 99, "Code": "GRP-1", "Name": "Project group", "Type": {"Id": 4}}},
    ]},
    f"/d2l/api/le/1.57/{ORG_UNIT}/content/toc": {"Modules": [{
        "ModuleId": 1, "Title": "Week 1", "Description": {"Text": "", "Html": ""}, "Modules": [],
        "Topics": [
            {"TopicId": 11, "Title": "Slides", "TypeIdentifier": "File", "Url": f"{COURSE}/slides.pdf"},
            {"TopicId": 12, "Title": "Reading", "TypeIdentifier": "File", "Url": f"{COURSE}/reading.html"},
            {"TopicId": 13, "Title": "Course site", "Type": "ExternalLink", "Url": "https://example.org/course"},
            {"TopicId": 14, "Title": "Welcome", "TypeIdentifier": "Link",
             "Url": f"/d2l/le/content/{ORG_UNIT}/viewContent/14/View"},
        ],
    }]},
    f"/d2l/api/le/1.57/{ORG_UNIT}/content/1/topics/14/": {"Body": "<p>Welcome to the course.</p>"},
    f"/d2l/api/le/1.57/{ORG_UNIT}/dropbox/folders/": [{
        "Id": 7, "Name": "Essay 1", "CategoryId": None,
        "StartDate": "2026-10-01T00:00:00.000Z", "DueDate": "2026-11-01T22:59:00.000Z",
        "CustomInstructions": {"Text": "Read the brief.",
                               "Html": f'<p><a href="{COURSE}/brief.pdf">Brief</a></p>'},
    }],
    f"/d2l/api/le/1.57/{ORG_UNIT}/news/": [{
        "Title": "Room change", "IsPublished": True, "CreatedDate": "2026-10-15T09:00:00.000Z",
        "Body": {"Text": "Lecture moves to Ruppert 040.", "Html": ""}, "Attachments": [],
    }],
    f"/d2l/api/le/1.57/{ORG_UNIT}/grades/values/myGradeValues/": [
        {"DisplayName": "Essay 1", "WeightedNumerator": 7.5, "WeightedDenominator": 10},
    ],
}

# Konjoin's project list, in the shape /api/project returns (made-up projects).
PROJECTS = [
    {"id": 3208, "slug": "3208-neurogenetics-elective", "label": "Neurogenetics elective",
     "status": {"id": 1, "label": "open"}, "public": True,
     "institute": {"label": "Department of Medicine"},
     "description": "<p>Why does one person develop a neurological disorder &amp; another not?</p>"
                    "<ul><li>Clinical genetics</li><li>Eight weeks</li></ul>",
     "leader": {"full_name": "Anoniem", "email": "leader@example.org", "organization_uid": "1000001"},
     "collaborators": [{"full_name": "Dr. A. Example", "email": "a.example@example.org",
                        "organization_uid": "1000002"}],
     "studies": [{"label": "Medicine"}], "types": [{"label": "Internship"}],
     "created_at": "2026-10-20T08:06:03.000000Z", "close_at": None},
    {"id": 3100, "slug": "3100-trade-routes-in-still-lifes", "label": "Trade routes in Dutch still lifes",
     "status": {"id": 1, "label": "open"}, "public": True,
     "institute": {"label": "Department of Information and Computing Sciences"},
     "description": "<p>Object recognition on 17th-century paintings.</p>",
     "leader": {"full_name": "Dr. B. Example"}, "collaborators": [], "studies": [], "types": [],
     "created_at": "2026-09-01T10:00:00.000000Z", "close_at": "2026-12-01T00:00:00.000000Z"},
    {"id": 2990, "slug": "2990-soil-microbes-and-drought", "label": "Soil microbes and drought",
     "status": {"id": 2, "label": "closed"}, "public": True,
     "institute": {"label": "Faculty of Geosciences"},
     "description": "<p>Field work in the Veluwe.</p>",
     "leader": {"full_name": "Prof. C. Example"}, "collaborators": [], "studies": [], "types": [],
     "created_at": "2026-05-01T10:00:00.000000Z", "close_at": None},
]
PER_PAGE = 2

# An article behind the library proxy; "View PDF" opens the PDF in a new tab, as ScienceDirect does,
# and "Download full issue" downloads the whole issue as a ZIP.
ARTICLE = ('{meta}<h1>A fake article</h1><a id="pdf" href="{pdf}" target="_blank">View PDF</a>'
           '<a id="issue" href="/issue/download">Download full issue</a>')
CITATION_PDF = '<meta name="citation_pdf_url" content="{url}">'
ARTICLE_PDF = b"%PDF-1.7\n% fake article\n"
ISSUE_ZIP = b"PK\x03\x04% fake issue\n"

# GitLab's sign-in page with its Solis-ID tab (LDAP), as served on 22-10-2026: GitLab's script
# renders the password field into the placeholder after the page loads.
GITLAB_SIGN_IN = """<head><meta name="csrf-token" content="{csrf}"></head>
<div class="flash-container">{flash}</div>
<form data-testid="new_ldap_user" action="/users/auth/ldapmain/callback" method="post">
  <input type="hidden" name="authenticity_token" value="{csrf}">
  <input id="ldapmain_username" name="username" type="text">
  <input class="js-password" data-id="ldapmain_password" data-name="password">
  <button data-testid="sign-in-button" type="submit">Sign in</button>
</form>
<script>
  document.querySelector(".js-password").outerHTML =
    '<input id="ldapmain_password" name="password" type="password">';
</script>"""
GITLAB_REFUSED = ('<div class="flash-alert">Could not authenticate you from Ldapmain because '
                  f'"Invalid credentials for {SOLISID}".</div>')
# The code step GitLab adds when you turned on its own two-factor login.
GITLAB_TWO_FACTOR = """<form action="/users/sign_in" method="post">
  <label for="user_otp_attempt">Verification code</label>
  <input id="user_otp_attempt" name="user[otp_attempt]" type="text">
</form>"""
GITLAB_HOME = """<head><meta name="csrf-token" content="{csrf}"></head><h1>Projects</h1>
<img src="https://secure.gravatar.com/avatar/0?s=32" alt="">"""
# GitLab's CSRF token belongs to the session: a page loaded before the login carries the guest's.
CSRF_GUEST, CSRF_SESSION = "csrf-guest", "csrf-session"
GITLAB_USER = {"id": 7, "username": "student", "name": "Test Student"}

# What Konjoin's real pages wait for before they count as loaded (seen 22-10-2026).
THIRD_PARTY = """<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Lato">
<script src="https://cdn.polyfill.io/v2/polyfill.min.js"></script>"""

# edu.nl's pages, trimmed from what the site served on 22-10-2026.
EDUNL_LOGOUT = '<div class="systray"><a href="/logout" aria-label="Logout">⏻</a></div>'
EDUNL_FORM = """<form name="short_url" method="post" novalidate="novalidate">
  <input type="url" id="short_url_longUrl" name="short_url[longUrl]" value="{value}">
  {error}
  <button class="btn btn-primary" type="submit">Shorten it!</button>
  <input type="hidden" id="short_url__token" name="short_url[_token]" value="{csrf}">
</form>"""
EDUNL_URL_ERROR = ('<span class="invalid-feedback d-block"><span class="form-error-message">Oops, this '
                   'is not quite a correct URL, possibly a typo or missing the http:// or https:// at '
                   'the beginning of the URL!</span></span>')
EDUNL_CREATED = """<h2>Your new link is ready to use</h2>
Your new link is:<br/><b>{short}</b><br/><br/>
This one refers to:<br/><b>{url}</b><br/><br/>
<a href="#" class="cpy" data-clipboard-text="{short}">Copy</a>
<form name="short_url" method="post" action="/manage/url/{code}/edit" class="form-inline">
  <input type="hidden" name="_token" value="{csrf}">
  <input type="hidden" name="short_url[longUrl]" value="{url}">
  <input type="text" id="labelfield" name="short_url[label]" value="" required maxlength="255">
  <button type="submit" class="btn btn-sm btn-secondary">Save</button>
  <input type="hidden" id="short_url__token" name="short_url[_token]" value="{csrf}">
</form>"""
EDUNL_ROW = """<tr>
  <td><button class="fabutton actieknop cpy" type="button" data-clipboard-text="{short}"></button>
      <a href="/manage/url/{code}/edit">edit</a></td>
  <th scope="row">{clicks}</th>
  <td><a target="_blank" href="{short}">{short_text}</a></td>
  <td>{date}</td>
  <td class="text-truncate telang"><a href="{url}" target="_blank"></a> {url}</td>
  <td class="text-truncate telang" title="{label}">{label}</td>
</tr>"""
EDUNL_CSRF = "edunl-csrf"
EDUNL_PER_PAGE = 2

# SURFfilesender: your id there and the API secret its profile page gives you (made up).
FILESENDER_USER = "0123456789abcdef0123456789abcdef01234567"
FILESENDER_SECRET = "fakesecret0fakesecret0fakesecret0fakesecret0fakesecret0fake0000"
# What "Download Python CLI Client configuration" serves (clidownload.php?config=1).
FILESENDER_INI = """[system]
base_url = https://filesender.surf.nl/rest.php
default_transfer_days_valid = 14

[user]
username = {user}
apikey = {secret}
"""


def make_jwt(exp: float) -> str:
    """An unsigned JWT with an 'exp' claim, enough for the CLI's expiry check."""
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': int(exp), 'sub': SOLISID})}.sig"


class FakeUU:
    def __init__(self) -> None:
        self.token = make_jwt(time.time() + 3600)
        self.codes: list[str] = []  # one-time codes the login form received
        self.logins = 0             # completed logins at Brightspace
        self.konjoin_logins = 0     # completed logins at Konjoin
        self.wayf_choices = 0       # times SURFconext's institution picker was answered
        self.library_logins = 0     # completed logins at the library proxy
        self.library_links: list[str] = []  # query strings of the proxy login links opened
        self.article_pdf = "/article/main.pdf"  # where the article's "View PDF" link points
        self.citation_pdf_url: str | None = "/article/main.pdf"  # the page's tag for search engines
        self.clicks_pdf = True      # you, at the window, click "View PDF" (when by_hand)
        self.clicks_issue = False   # you click "Download full issue" instead
        self.elsewhere: list[str] = []  # requests for hosts this fake doesn't serve (aborted)
        self.gitlab_logins = 0      # completed sign-ins at GitLab
        self.gitlab_two_factor = False  # you turned on GitLab's own two-factor login
        self.gitlab_api: list[str] = []  # "METHOD /path" of every API request GitLab received
        self.gitlab_created: list[dict] = []  # what the CLI sent to create each project
        self.gitlab_projects: list[dict] = []
        self.gitlab_keys: list[dict] = []
        self.edunl_logins = 0       # completed logins at edu.nl
        self.edunl_links: list[dict] = []  # your links, newest first: code, url, label, clicks, date
        self.edunl_posts: list[dict] = []  # every form edu.nl received
        self.filesender_logins = 0  # completed logins at SURFfilesender
        self.filesender_secret = FILESENDER_SECRET  # "" when your profile has none

    def install(self, ctx, by_hand: bool = False) -> None:
        """Serve the fake to ctx. by_hand: someone at this browser fills in UU's forms (--auth browser)."""
        ctx.route("**/*", lambda route: self._handle(route, by_hand))

    # ── dispatch ──────────────────────────────────────────────────────────────
    def _handle(self, route, by_hand: bool = False) -> None:
        req = route.request
        url = urlparse(req.url)
        origin = f"{url.scheme}://{url.netloc}"
        handler = {
            IDP: lambda *a: self._idp(*a, by_hand=by_hand),
            BRIGHTSPACE: self._brightspace,
            KONJOIN: self._konjoin,
            SURFCONEXT: self._surfconext,
            LIBRARY: lambda *a: self._library(*a, by_hand=by_hand),
            GITLAB: lambda *a: self._gitlab(*a, by_hand=by_hand),
            EDUNL: self._edunl,
            FILESENDER: self._filesender,
        }.get(origin)
        if handler:
            handler(route, req, url)
        else:
            self.elsewhere.append(req.url)
            route.abort()

    # ── UU's login (the identity provider) ────────────────────────────────────
    def _idp(self, route, req, url, by_hand: bool = False) -> None:
        if url.path != "/nidp/app/login":
            return route.fulfill(status=404, body="not found")
        query = parse_qs(url.query)
        sp = query.get("sp", ["brightspace"])[0]
        if req.method == "GET":
            if sp in VIA_SURFCONEXT:  # only SURFconext's picker links here
                self.wayf_choices += 1
            if "IDP_SESSION=" in _cookies(req):  # single sign-on: still logged in at UU
                return _redirect(route, ACS[sp])
            you = _by_hand({"Ecom_User_ID": SOLISID, "Ecom_Password": PASSWORD}) if by_hand else ""
            return _html(route, LOGIN_FORM.format(sp=sp) + you)
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        if query.get("sid") == ["0"]:
            if form.get("Ecom_User_ID") == SOLISID and form.get("Ecom_Password") == PASSWORD:
                you = _by_hand({"nffc": pyotp.TOTP(TOTP_SECRET).now()}) if by_hand else ""
                return _html(route, OTP_FORM.format(sp=sp) + you)
            return _html(route, "<p>Wrong Solis-id or password.</p>" + LOGIN_FORM.format(sp=sp))
        code = form.get("nffc", "")
        self.codes.append(code)
        if pyotp.TOTP(TOTP_SECRET).verify(code, valid_window=1):
            return _redirect(route, ACS[sp], cookie="IDP_SESSION=1; Path=/")
        return _html(route, "<p>Wrong code.</p>" + OTP_FORM.format(sp=sp))

    # ── SURFconext, in front of Konjoin and edu.nl ────────────────────────────
    def _surfconext(self, route, req, url) -> None:
        if url.path.startswith("/authentication/idp/single-sign-on"):
            sp = parse_qs(url.query).get("sp", ["konjoin"])[0]  # who asked, as the real request says
            return _html(route, WAYF.format(sp=sp))
        route.fulfill(status=404, body="not found")

    # ── Brightspace ───────────────────────────────────────────────────────────
    def _brightspace(self, route, req, url) -> None:
        path = url.path
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=CORS)
        if path.startswith("/d2l/api/"):
            return self._api(route, req, path)
        if path == "/d2l/lp/auth/saml/acs":
            self.logins += 1
            return _redirect(route, f"{BRIGHTSPACE}/d2l/home", cookie="d2lSessionVal=s3ss; Path=/")
        if "d2lSessionVal=" not in _cookies(req):
            return _redirect(route, f"{IDP}/nidp/app/login?sp=brightspace")
        if path in ("/", "/d2l/home"):
            return _html(route, HOME.replace("TOKEN", self.token))
        name = path.rsplit("/", 1)[-1]
        if path.startswith(COURSE + "/") and name.endswith(".pdf"):
            return route.fulfill(status=200, body=PDF, headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": f'attachment; filename="{name}"',
            })
        if path == f"{COURSE}/reading.html":
            return _html(route, READING)
        route.fulfill(status=404, body="not found")

    def _api(self, route, req, path) -> None:
        if req.all_headers().get("authorization") != f"Bearer {self.token}":
            return _json(route, {"Errors": [{"Message": "Unauthorized"}]}, status=401)
        if path not in API:
            return _json(route, {"Errors": [{"Message": "Not found"}]}, status=404)
        _json(route, API[path])

    # ── Konjoin ───────────────────────────────────────────────────────────────
    def _konjoin(self, route, req, url) -> None:
        path, query = url.path, parse_qs(url.query)
        logged_in = "laravel_session=" in _cookies(req)
        if path == "/saml2/login":
            return _redirect(route, f"{SURFCONEXT}/authentication/idp/single-sign-on?SAMLRequest=x")
        if path == "/saml2/acs":
            self.konjoin_logins += 1
            return _redirect(route, f"{KONJOIN}/", cookie="laravel_session=k0nj; Path=/")
        if path == "/":
            user = ('<meta name="user-id" content="42"><meta name="user-name" content="Test Student">'
                    if logged_in else "")
            return _html(route, f"<head>{user}{THIRD_PARTY}</head><h1>KonJoin</h1>")
        if path == "/api/project":
            term = query.get("query", [""])[0].lower()
            found = [p for p in PROJECTS if term in p["label"].lower()]
            page = int(query.get("page", ["1"])[0])
            last = max(1, -(-len(found) // PER_PAGE))
            return _json(route, {
                "current_page": page, "last_page": last, "per_page": PER_PAGE, "total": len(found),
                "data": found[(page - 1) * PER_PAGE:page * PER_PAGE],
            })
        if path.startswith("/api/project/"):
            if not logged_in:
                return _json(route, {"message": "Unauthenticated."}, status=401)
            slug = path.rsplit("/", 1)[-1]
            project = next((p for p in PROJECTS if p["slug"] == slug), None)
            if project is None:
                return _json(route, {"message": "Not found."}, status=404)
            return _json(route, project)
        route.fulfill(status=404, body="not found")

    # ── the library proxy (EZproxy) ───────────────────────────────────────────
    def _library(self, route, req, url, by_hand: bool = False) -> None:
        path = url.path
        logged_in = "ezproxy=" in _cookies(req)
        if path == "/login":
            self.library_links.append(url.query)
            target = urlparse(parse_qs(url.query).get("url", [""])[0])
            if logged_in and target.path.endswith(".pdf"):  # a publisher's PDF, proxied
                return _redirect(route, f"{LIBRARY}{target.path}")
            if logged_in:
                return _redirect(route, f"{LIBRARY}/article")
            return _redirect(route, f"{IDP}/nidp/app/login?sp=library")
        if path == "/Shibboleth.sso/SAML2/POST":
            self.library_logins += 1
            return _redirect(route, f"{LIBRARY}/article", cookie="ezproxy=l1b; Path=/")
        if not logged_in:
            return route.fulfill(status=403, body="not logged in at the proxy")
        if path == "/article":
            meta = CITATION_PDF.format(url=self.citation_pdf_url) if self.citation_pdf_url else ""
            # You, at the window: click "View PDF", or "Download full issue".
            link = "issue" if self.clicks_issue else "pdf"
            you = f"<script>setTimeout(() => document.getElementById('{link}').click(), 300)</script>"
            clicks = by_hand and (self.clicks_pdf or self.clicks_issue)
            return _html(route, ARTICLE.format(meta=meta, pdf=self.article_pdf) + (you if clicks else ""))
        if path == self.article_pdf or path.startswith("/content/pdf/"):  # SpringerLink's PDFs
            return route.fulfill(status=200, body=ARTICLE_PDF,
                                 headers={"Content-Type": "application/pdf"})
        if path == "/issue/download":
            return route.fulfill(status=200, body=ISSUE_ZIP, headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="issue 12.zip"',
            })
        route.fulfill(status=404, body="not found")

    # ── GitLab (git.science.uu.nl) ────────────────────────────────────────────
    def _gitlab(self, route, req, url, by_hand: bool = False) -> None:
        path = url.path
        logged_in = "_gitlab_session=in" in _cookies(req)
        if path.startswith("/api/v4/"):
            return self._gitlab_api(route, req, path.removeprefix("/api/v4"), logged_in)
        if path == "/users/auth/ldapmain/callback" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
            if (form.get("username"), form.get("password")) != (SOLISID, PASSWORD):
                return _redirect(route, f"{GITLAB}/users/sign_in?failed=1")
            if self.gitlab_two_factor:
                return _html(route, GITLAB_TWO_FACTOR)
            self.gitlab_logins += 1
            return _redirect(route, f"{GITLAB}/", cookie="_gitlab_session=in; Path=/")
        if path not in ("/", "/users/sign_in"):
            return route.fulfill(status=404, body="not found")
        if logged_in:
            if path == "/users/sign_in":
                return _redirect(route, f"{GITLAB}/")
            return _html(route, GITLAB_HOME.format(csrf=CSRF_SESSION))
        # A guest gets the sign-in page, at / too: GitLab's server-side redirect, in one step.
        failed = "failed" in parse_qs(url.query)
        page = GITLAB_SIGN_IN.format(csrf=CSRF_GUEST, flash=GITLAB_REFUSED if failed else "")
        if by_hand and path == "/users/sign_in" and not failed:
            page += _by_hand({"ldapmain_username": SOLISID, "ldapmain_password": PASSWORD})
        _html(route, page)

    def _gitlab_api(self, route, req, path, logged_in: bool) -> None:
        self.gitlab_api.append(f"{req.method} {path}")
        # A session cookie counts for a change only with the session's CSRF token.
        verified = req.method in ("GET", "HEAD") or req.all_headers().get("x-csrf-token") == CSRF_SESSION
        if not (logged_in and verified):
            return _json(route, {"message": "401 Unauthorized"}, status=401)
        body = json.loads(req.post_data or "null")
        user = GITLAB_USER["username"]
        if (req.method, path) == ("GET", "/user"):
            return _json(route, GITLAB_USER)
        if (req.method, path) == ("GET", "/projects"):
            return _json(route, self.gitlab_projects)
        if (req.method, path) == ("POST", "/projects"):
            slug = body["name"].lower().replace(" ", "-")
            if any(p["path"] == slug for p in self.gitlab_projects):
                return _json(route, {"message": {"name": ["has already been taken"],
                                                 "path": ["has already been taken"]}}, status=400)
            self.gitlab_created.append(body)
            project = {"id": len(self.gitlab_projects) + 1, "name": body["name"], "path": slug,
                       "path_with_namespace": f"{user}/{slug}",
                       "visibility": body.get("visibility", "private"),
                       "web_url": f"{GITLAB}/{user}/{slug}",
                       "ssh_url_to_repo": f"git@git.science.uu.nl:{user}/{slug}.git",
                       "http_url_to_repo": f"{GITLAB}/{user}/{slug}.git"}
            self.gitlab_projects.insert(0, project)
            return _json(route, project, status=201)
        if (req.method, path) == ("POST", "/user/keys"):
            if any(k["key"] == body["key"] for k in self.gitlab_keys):
                return _json(route, {"message": {"key": ["has already been taken"]}}, status=400)
            self.gitlab_keys.append(body)
            return _json(route, {"id": len(self.gitlab_keys), **body}, status=201)
        _json(route, {"message": "404 Not Found"}, status=404)

    # ── edu.nl ────────────────────────────────────────────────────────────────
    def _edunl(self, route, req, url) -> None:
        path, query = url.path, parse_qs(url.query)
        if path == "/mellon/postResponse":
            self.edunl_logins += 1
            return _redirect(route, f"{EDUNL}/manage/", cookie="mellon-cookie=m3ll0n; Path=/")
        if not path.startswith("/manage/"):
            return route.fulfill(status=404, body="not found")
        if "mellon-cookie=" not in _cookies(req):
            return _redirect(route, f"{SURFCONEXT}/authentication/idp/single-sign-on?SAMLRequest=x&sp=edunl")
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()} if req.method == "POST" else {}
        if req.method == "POST":
            self.edunl_posts.append({"path": path, **form})
            if EDUNL_CSRF not in (form.get("short_url[_token]"), form.get("_token")):
                return route.fulfill(status=400, body="The CSRF token is invalid.")

        if (req.method, path) == ("POST", "/manage/"):
            long_url = form.get("short_url[longUrl]", "")
            if not long_url.startswith(("http://", "https://")) or "." not in (urlparse(long_url).hostname or ""):
                return _html(route, EDUNL_LOGOUT + EDUNL_FORM.format(
                    value=long_url, error=EDUNL_URL_ERROR, csrf=EDUNL_CSRF))
            code = f"c{len(self.edunl_links) + 1:04d}"
            self.edunl_links.insert(0, {"code": code, "url": long_url, "label": "", "clicks": 0,
                                        "date": "2026-10-22"})
            return _redirect(route, f"{EDUNL}/manage/url/{code}/show")

        one = re.fullmatch(r"/manage/url/([^/]+)/(show|edit)", path)
        if one:
            link = next((x for x in self.edunl_links if x["code"] == one[1]), None)
            if link is None:
                return route.fulfill(status=404, body="not found")
            if one[2] == "show":
                return _html(route, EDUNL_LOGOUT + EDUNL_CREATED.format(
                    short=f"{EDUNL}/{link['code']}", url=link["url"], code=link["code"], csrf=EDUNL_CSRF))
            if req.method == "POST":
                link["label"] = form.get("short_url[label]", "")
                return _redirect(route, f"{EDUNL}/manage/?updated=1")

        overview = re.fullmatch(r"/manage/(?:page/(\d+))?", path)
        if overview and req.method == "GET":
            n = int(overview[1] or 1)
            term = query.get("filterValue", [""])[0].lower()
            found = [x for x in self.edunl_links if term in x["url"].lower() or term in x["label"].lower()]
            rows = "".join(EDUNL_ROW.format(short=f"{EDUNL}/{x['code']}", short_text=f"edu.nl/{x['code']}", **x)
                           for x in found[(n - 1) * EDUNL_PER_PAGE:n * EDUNL_PER_PAGE])
            more = len(found) > n * EDUNL_PER_PAGE
            alert = ('<div class="messages"><div class="alert alert-success">The URL was updated.</div></div>'
                     if "updated" in query else "")
            nav = f'<a href="/manage/page/{n + 1}">{n + 1}</a>' if more else ""
            return _html(route, EDUNL_LOGOUT + alert + EDUNL_FORM.format(value="", error="", csrf=EDUNL_CSRF)
                         + f'<table><tbody data-test="links">{rows}</tbody></table>'
                         + f'<div class="navigation">{nav}</div>')
        route.fulfill(status=404, body="not found")

    # ── SURFfilesender's login and profile (its REST API is faked in test_filesender.py) ──
    def _filesender(self, route, req, url) -> None:
        path, query = url.path, parse_qs(url.query)
        logged_in = "SimpleSAMLAuthToken=" in _cookies(req)
        if path.startswith("/simplesaml/module.php/saml/sp/login/"):
            return _redirect(route, f"{SURFCONEXT}/authentication/idp/single-sign-on?SAMLRequest=x&sp=filesender")
        if path == urlparse(ACS["filesender"]).path:
            self.filesender_logins += 1
            return _redirect(route, f"{FILESENDER}/?s=user", cookie="SimpleSAMLAuthToken=t0k; Path=/")
        if path == "/":
            if logged_in:
                return _html(route, "<h1>My profile</h1><h2>Remote authentication</h2>"
                                    '<a href="clidownload.php?config=1">Download Python CLI Client configuration</a>')
            return _html(route, '<h1>Welkom bij SURFfilesender!</h1><a id="btn_logon" href="#">Aanmelden</a>')
        if path == "/clidownload.php" and query.get("config") == ["1"]:
            if not logged_in:
                return route.fulfill(status=500, body="Authentication required")
            ini = FILESENDER_INI.format(user=FILESENDER_USER, secret=self.filesender_secret)
            return route.fulfill(status=200, body=ini, headers={"Content-Type": "text/plain; charset=utf-8"})
        route.fulfill(status=404, body="not found")


def _by_hand(values: dict) -> str:
    """You, at --auth browser's window: fill in the form on the page and send it."""
    fill = "".join(f"document.getElementById({json.dumps(k)}).value = {json.dumps(v)};"
                   for k, v in values.items())
    return f"<script>setTimeout(() => {{ {fill} document.forms[0].submit(); }}, 300)</script>"


def _cookies(req) -> str:
    return req.all_headers().get("cookie", "")


def _html(route, body: str) -> None:
    # Pad past the CLI's "tiny page that mentions login" check, which real pages never trip.
    page = f"<!doctype html><html><body>{body}<!-- {'.' * 600} --></body></html>"
    route.fulfill(status=200, body=page, headers={"Content-Type": "text/html; charset=utf-8"})


def _json(route, data, status: int = 200) -> None:
    route.fulfill(status=status, body=json.dumps(data),
                  headers={"Content-Type": "application/json", **CORS})


def _redirect(route, location: str, cookie: str | None = None) -> None:
    # A client-side hop, never a 302: Playwright routes only the first URL of a server redirect,
    # so the follow-up request would bypass this fake and go out to the real host.
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if cookie:
        headers["Set-Cookie"] = cookie
    body = f"<!doctype html><html><head><script>location.replace({json.dumps(location)})</script></head></html>"
    route.fulfill(status=200, headers=headers, body=body)
