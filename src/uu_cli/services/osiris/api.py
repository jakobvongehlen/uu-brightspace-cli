"""Osiris Student's public course catalogue (osiris-student.uu.nl/onderwijscatalogus).

No login: the catalogue page itself calls two open endpoints. Search is an Elasticsearch query,
POSTed as is to /student/cursussen/zoeken; a course's page is /owc/cursussen/<id_cursus>, a tree
of sections ("rubrieken") whose fields hold text, HTML or tables. Both answer in the language of
the `taal` header (EN or NL), and the search fails with an "interne fout" without it.
"""

from __future__ import annotations

import html
import re

import requests

BASE_URL = "https://osiris-student.uu.nl"
API = f"{BASE_URL}/student/osiris"
SEARCH = f"{API}/student/cursussen/zoeken"
CRITERIA = f"{API}/owc/zoekcriteria/cursus/owc_extern?limit=9999"
COURSE = API + "/owc/cursussen/{}"
CATALOGUE = f"{BASE_URL}/onderwijscatalogus/extern/cursus"

# --flag -> (index field, nested path or None, title). The values come from the index itself
# (`uu osiris filters`), in the language asked for.
FILTERS = {
    "faculty": ("faculteit_naam", None, "Faculty"),
    "level": ("categorie_omschrijving", None, "Category / level"),
    "period": ("blokken_nested.periode_omschrijving", "blokken_nested", "Starting period"),
    "timeslot": ("blokken_nested.timeslots.timeslot", "blokken_nested", "Timeslot"),
    "language": ("voertalen.voertaal_omschrijving", None, "Language"),
    "type": ("cursustype_omschrijving", None, "Course type"),
}
# Periods also come as bare dates ("07-09-2026 t/m 13-11-2026"); the site hides those as filters.
DATES_ONLY = r"[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9] .*"

# What a search result keeps of a course.
FIELDS = ["id_cursus", "cursus", "collegejaar", "cursus_korte_naam", "cursus_lange_naam",
          "faculteit_naam", "punten", "punteneenheid", "categorie_omschrijving",
          "cursustype_omschrijving", "voertalen", "blokken", "opmerking_cursus", "docenten",
          "inschrijfperiodes_cursus"]

TIMEOUT_S = 30


class OsirisError(Exception):
    """Osiris didn't answer as expected; the message says what it said."""


def _headers(lang: str) -> dict:
    return {"taal": lang.upper(), "client_type": "web", "Accept": "application/json"}


def get(url: str, lang: str = "en"):
    try:
        r = requests.get(url, headers=_headers(lang), timeout=TIMEOUT_S)
    except requests.RequestException as err:
        raise OsirisError(f"Osiris didn't answer: {err}") from None
    return _json(r)


def post(url: str, body: dict, lang: str = "en"):
    try:
        r = requests.post(url, json=body, headers=_headers(lang), timeout=TIMEOUT_S)
    except requests.RequestException as err:
        raise OsirisError(f"Osiris didn't answer: {err}") from None
    return _json(r)


def _json(r):
    try:
        body = r.json()
    except ValueError:
        raise OsirisError(f"Osiris answered HTTP {r.status_code} without JSON") from None
    if isinstance(body, dict) and body.get("result") == "FAILED":
        raise OsirisError(f"Osiris: {(body.get('failure') or {}).get('message', 'failed')}")
    if not r.ok:
        raise OsirisError(f"Osiris answered HTTP {r.status_code}")
    return body


def current_year(lang: str = "en") -> str:
    """The academic year the catalogue opens on, e.g. "2026-2027"."""
    for c in get(CRITERIA, lang).get("items", []):
        if c.get("filter") == "collegejaar" and c.get("waarde"):
            return c["waarde"]
    raise OsirisError("Osiris didn't say which academic year its catalogue shows")


def year(given: str | None, lang: str = "en") -> str:
    """--year as the index writes it: 2026 or 2026-2027 -> "2026-2027"; none -> the current one."""
    if not given:
        return current_year(lang)
    m = re.fullmatch(r"(\d{4})(?:[-/](\d{4}|\d{2}))?", given)
    if not m or (m[2] and int(m[2][-2:]) != (int(m[1]) + 1) % 100):
        raise ValueError(f"--year {given}: write it as 2026 or 2026-2027")
    return f"{m[1]}-{int(m[1]) + 1}"


def filter_values(year: str, lang: str = "en") -> dict[str, list[tuple[str, int]]]:
    """Each filter's values in that year, with how many courses have them."""
    aggs = {}
    for name, (field, nested, _) in FILTERS.items():
        terms = {"terms": {"field": field, "size": 2500}}
        if name == "period":
            terms["terms"]["exclude"] = DATES_ONLY
        aggs[name] = {"nested": {"path": nested}, "aggs": {"x": terms}} if nested else terms
    body = {"size": 0, "query": {"bool": {"must": [_year(year)]}}, "aggs": aggs}
    out = {}
    for name, agg in post(SEARCH, body, lang)["aggregations"].items():
        buckets = agg["x"]["buckets"] if "x" in agg else agg["buckets"]
        out[name] = [(b["key"], b["doc_count"]) for b in buckets if b["key"]]
    return out


def choose(given: str, values: list[str], flag: str) -> str:
    """The value meant by GIVEN: the value itself, its code or name ("M" or "master" for
    "M (Master)"), a whole word of it ("science"), or else a part of it; any case. Raises ValueError unless exactly one."""
    g = given.strip().lower()
    for match in (lambda v: v.lower() == g,
                  lambda v: v.split(" (")[0].lower() == g,
                  lambda v: v.lower().endswith(f"({g})"),
                  lambda v: re.search(rf"(?<!\w){re.escape(g)}(?!\w)", v.lower()),
                  lambda v: g in v.lower()):
        found = [v for v in values if match(v)]
        if len(found) == 1:
            return found[0]
        if found:
            raise ValueError(f"--{flag} {given} matches several: " + "; ".join(found))
    raise ValueError(f"--{flag} {given} matches none of: " + "; ".join(values))


def query(text: str | None, year: str, filters: dict[str, list[str]] | None = None,
          teacher: str | None = None, open_now: bool = False) -> dict:
    """The Elasticsearch query the catalogue page would send. FILTERS values within one flag are
    alternatives; different flags must all hold."""
    must = [_year(year)]
    if text:
        # As the page does: the start of a word in the code or a name
        must.append({"multi_match": {"query": text.upper(), "type": "phrase_prefix",
                                     "fields": ["cursus", "cursus_korte_naam", "cursus_lange_naam"],
                                     "max_expansions": 200}})
    for name, values in (filters or {}).items():
        if not values:
            continue
        field, nested, _ = FILTERS[name]
        clause = {"terms": {field: list(values)}}
        must.append({"nested": {"path": nested, "query": clause}} if nested else clause)
    if teacher:
        # docenten.docent is one keyword ("Stellato, R.K. (Rebecca)"), so only a wildcard finds part of it
        must.append({"wildcard": {"docenten.docent": {"value": f"*{teacher.strip()}*",
                                                      "case_insensitive": True}}})
    if open_now:
        must += [{"range": {"inschrijfperiodes_cursus.datum_vanaf": {"lte": "now"}}},
                 {"range": {"inschrijfperiodes_cursus.datum_tm": {"gte": "now"}}}]
    return {"bool": {"must": must}}


def search(q: dict, size: int = 25, start: int = 0, lang: str = "en") -> tuple[int, list[dict]]:
    """(how many courses match, the courses from START on): best match first, then by name."""
    body = {"from": start, "size": size, "_source": FIELDS, "query": q,
            "sort": ["_score", {"cursus_lange_naam.raw": {"order": "asc"}},
                     {"cursus": {"order": "asc"}}],
            "track_total_hits": True}
    hits = post(SEARCH, body, lang)["hits"]
    return hits["total"]["value"], [summary(h["_source"]) for h in hits["hits"]]


def summary(src: dict) -> dict:
    """A search hit as the CLI shows and prints it as JSON."""
    periods = []
    for b in src.get("blokken") or []:
        p = (b.get("periode_lange_omschrijving") or b.get("periode_omschrijving") or "").strip()
        if p and p not in periods:
            periods.append(p)
    return {
        "code": src.get("cursus"),
        "id": src.get("id_cursus"),
        "year": src.get("collegejaar"),
        "name": (src.get("cursus_lange_naam") or src.get("cursus_korte_naam") or "").strip(),
        "ec": _ec(src.get("punten")),
        "faculty": src.get("faculteit_naam"),
        "level": src.get("categorie_omschrijving"),
        "type": src.get("cursustype_omschrijving"),
        "languages": [v["voertaal_omschrijving"] for v in src.get("voertalen") or []
                      if v.get("voertaal_omschrijving")],
        "periods": periods,
        "timeslots": sorted({t["timeslot"] for b in src.get("blokken") or []
                             for t in b.get("timeslots") or [] if t.get("timeslot")}),
        "teachers": list(dict.fromkeys(d["docent"] for d in src.get("docenten") or []
                                       if d.get("docent"))),
        "note": (src.get("opmerking_cursus") or "").strip(),
        "url": f"{CATALOGUE}?cursuscode={src.get('cursus')}&collegejaar={(src.get('collegejaar') or '')[:4]}",
    }


def _ec(points) -> str:
    if points in (None, ""):
        return ""
    try:
        return f"{float(str(points).replace(',', '.')):g}"
    except ValueError:
        return str(points)


def find(code: str, year: str, lang: str = "en") -> dict | None:
    """The course with exactly this code in that year, as `summary` gives it."""
    body = {"size": 50, "_source": FIELDS,
            "query": {"bool": {"must": [_year(year), {"match": {"cursus": code}}]}}}
    for h in post(SEARCH, body, lang)["hits"]["hits"]:
        if (h["_source"].get("cursus") or "").upper() == code.upper():
            return summary(h["_source"])
    return None


def course(id_cursus: int, lang: str = "en") -> list[dict]:
    """A course's page: its sections, each {"section", "title", "fields"}; see `sections`."""
    return sections(get(COURSE.format(int(id_cursus)), lang).get("items", []))


# Sections and fields the page itself shows elsewhere or not at all.
SKIP_SECTIONS = {"rubriek-zoek", "rubriek-kop"}
SKIP_FIELDS = {"item-docent-cursussen", "item-url-plaatje"}
# Sections the page shows without a heading.
UNTITLED = {"rubriek-links": "Links"}


def sections(items: list[dict]) -> list[dict]:
    """The page's sections as plain data: each field is {"title", "text"} or, for a table,
    {"title", "rows": [{"title", "fields"}]}; links are fields with a "url"."""
    out = []
    for r in items:
        if r.get("rubriek") in SKIP_SECTIONS:
            continue
        fields = _fields(r.get("velden") or [])
        if fields:
            title = (r.get("titel") or "").strip() or UNTITLED.get(r.get("rubriek"), "")
            out.append({"section": r.get("rubriek"), "title": title,
                        "fields": fields})
    return out


def _fields(velden: list[dict]) -> list[dict]:
    out = []
    for f in velden:
        if f.get("veld") in SKIP_FIELDS:
            continue
        title = (f.get("titel") or "").strip()
        if "docent" in f:                       # a lecturer: name, e-mail and their other courses
            inner = _fields(f.get("velden") or [])
            mail = next((x["text"] for x in inner if "mail" in x.get("title", "").lower()), "")
            out.append({"title": "", "text": f"{f['docent']} <{mail.lower()}>" if mail else f["docent"]})
            continue
        if "url" in f:
            out.append({"title": title, "url": f["url"]})
            continue
        value = f.get("waarde")
        if isinstance(value, dict):             # a region: fields grouped side by side
            out += _fields(value.get("velden") or [])
        elif isinstance(value, list):           # a table: rows with a heading and their own fields
            rows = []
            for row in value:
                inner = _fields(row.get("velden") or [])
                heading = (row.get("omschrijving") or "").strip()
                if heading or inner:
                    rows.append({"title": heading, "fields": inner})
            if rows:
                out.append({"title": title, "rows": rows})
        elif "velden" in f and not value:       # a region written without "waarde"
            out += _fields(f["velden"])
        else:
            text = to_text(value)
            if text:
                out.append({"title": title, "text": text})
    return out


def to_text(value) -> str:
    """Osiris's HTML (or plain value) as readable text: paragraphs and list items on their own lines."""
    if value is None:
        return ""
    s = str(value)
    if "<" not in s:
        return html.unescape(s).strip()
    s = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "\n• ", s)
    s = re.sub(r"(?i)</?(p|div|ul|ol|tr|h\d|table|small)\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _year(year: str) -> dict:
    return {"terms": {"collegejaar": [year]}}
