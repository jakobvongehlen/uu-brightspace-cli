"""Osiris's course catalogue against a fake of its two open endpoints (no browser, no network)."""

from __future__ import annotations

import json

import pytest

from uu_cli.services.osiris import api
from uu_cli.services.osiris.cli import main as osiris

YEAR = "2026-2027"


def course(code, name, year=YEAR, **kw):
    src = {"id_cursus": kw.pop("id", abs(hash((code, year))) % 100000), "cursus": code,
           "collegejaar": year, "cursus_lange_naam": name, "cursus_korte_naam": name[:20],
           "faculteit_naam": "Faculty of Science", "punten": "7.5", "punteneenheid": "EC",
           "categorie_omschrijving": "M (Master)", "cursustype_omschrijving": "Course",
           "voertalen": [{"voertaal_omschrijving": "English"}],
           "blokken": [{"periode_omschrijving": "Period 2",
                        "periode_lange_omschrijving": "Period 2 (16-11-2026 until 05-02-2027)",
                        "timeslots": [{"timeslot": "D"}]}],
           "docenten": [{"docent": "Stellato, R.K. (Rebecca)"}]}
    src.update(kw)
    return src


COURSES = [
    course("INFOMEMD", "Epidemiology and Medical Datascience", id=249361),
    course("INFOMEMD", "Epidemiology and Medical Datasciencea", year="2025-2026", id=244282),
    course("INFOMEMDX", "Epidemiology extra", id=249362),
    course("WISB263", "Mathematical statistics", categorie_omschrijving="2 (Bachelor Elaborating)",
           docenten=[]),
]

AGGS = {
    "faculty": ["Faculty of Science", "Faculty of Social Sciences", "Faculty of Humanities"],
    "level": ["M (Master)", "HM (Master Honours)", "2 (Bachelor Elaborating)", ""],
    "period": ["Period 1", "Period 2", "Block 11A-12W"],
    "timeslot": ["A (MON-morning, WED-afternoon)", "D", "Timeslot not applicable"],
    "language": ["English", "Dutch"],
    "type": ["Course", "Placement"],
}

PAGE = {"items": [
    {"rubriek": "rubriek-links", "velden": [{"veld": "item-links", "waarde": [{"velden": [
        {"titel": "Schedule", "url": "https://mytimetable.uu.nl/link?timetable.key=INFOMEMD"}]}]}]},
    {"rubriek": "rubriek-kop", "titel": "", "velden": [{"veld": "cursus", "waarde": "INFOMEMD"}]},
    {"rubriek": "rubriek-praktische-info", "titel": "General information", "velden": [
        {"veld": "regio-links", "titel": "", "waarde": {"velden": [
            {"veld": "item-punten", "titel": "Credits", "waarde": "7.5 EC"}]}},
        {"veld": "regio-rechts", "titel": "", "waarde": {"velden": [
            {"veld": "item-aangeboden", "titel": "Offered by",
             "waarde": "<html><head><title></title><style>body {}</style></head>"
                       "<body>Utrecht University&nbsp;- Faculty of Science</body></html>"}]}}]},
    {"rubriek": "rubriek-inhoud", "titel": "Course description", "velden": [
        {"veld": "item-inhoud-1", "titel": "Course goals",
         "waarde": "<p>Students can:</p><ul><li>use mixed models</li><li>impute &amp; pool</li></ul>"}]},
    {"rubriek": "rubriek-docenten", "titel": "Lecturers", "velden": [
        {"veld": "tabel-docenten", "titel": "Lecturers", "waarde": [
            {"omschrijving": "Course contact", "velden": [
                {"docent": "dr. R.K. Stellato", "url_medewerker": "https://www.uu.nl/medewerkers/x",
                 "velden": [{"veld": "regio-docent-contact", "titel": "", "velden": [
                     {"veld": "item-docent-e-mail", "titel": "Email-address", "waarde": "R.K.Stellato@UU.NL"},
                     {"veld": "item-docent-cursussen", "titel": "Courses", "waarde": [
                         {"id_cursus": 1, "cursus": "OTHER"}]}]}]}]}]}]},
    {"rubriek": "rubriek-materialen", "titel": "Materials", "velden": []},
]}


class FakeOsiris:
    """The catalogue's endpoints: a tiny Elasticsearch over COURSES, the criteria and one page."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.fail = None

    def get(self, url, headers=None, timeout=None):
        assert headers.get("taal") in ("EN", "NL")
        if url == api.CRITERIA:
            return Response({"items": [{"filter": "trefwoord", "waarde": None},
                                       {"filter": "collegejaar", "waarde": YEAR}]})
        if url == api.COURSE.format(249361):
            return Response(PAGE)
        return Response({"result": "FAILED", "failure": {"message": "niet gevonden"}}, 404)

    def post(self, url, json=None, headers=None, timeout=None):
        assert url == api.SEARCH
        if "taal" not in (headers or {}) or self.fail:
            return Response({"result": "FAILED", "failure": {
                "message": self.fail or "Er is een interne fout opgetreden in OSIRIS Link."}}, 500)
        self.bodies.append(json)
        if "aggs" in json:
            aggs = {}
            for name, values in AGGS.items():
                buckets = {"buckets": [{"key": v, "doc_count": 3} for v in values]}
                aggs[name] = {"doc_count": 9, "x": buckets} if api.FILTERS[name][1] else buckets
            return Response({"hits": {"total": {"value": 4}, "hits": []}, "aggregations": aggs})
        hits = [c for c in COURSES if self._matches(c, json["query"])]
        page = hits[json.get("from", 0):][:json.get("size", 10)]
        return Response({"hits": {"total": {"value": len(hits)},
                                  "hits": [{"_source": c} for c in page]}})

    def _matches(self, c, q):
        for clause in q["bool"]["must"]:
            if "terms" in clause and "collegejaar" in clause["terms"]:
                if c["collegejaar"] not in clause["terms"]["collegejaar"]:
                    return False
            elif "multi_match" in clause:
                text = clause["multi_match"]["query"].lower()
                if not any(w.startswith(text) for w in (c["cursus"] + " " + c["cursus_lange_naam"]).lower().split()):
                    return False
            elif "match" in clause:
                if clause["match"]["cursus"].lower() not in c["cursus"].lower():
                    return False
            elif "terms" in clause and "categorie_omschrijving" in clause["terms"]:
                if c["categorie_omschrijving"] not in clause["terms"]["categorie_omschrijving"]:
                    return False
            elif "wildcard" in clause:
                part = clause["wildcard"]["docenten.docent"]["value"].strip("*").lower()
                if not any(part in d["docent"].lower() for d in c["docenten"]):
                    return False
        return True


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, 200 <= status < 300

    def json(self):
        return self.body


@pytest.fixture
def fake(monkeypatch):
    f = FakeOsiris()
    monkeypatch.setattr(api.requests, "get", f.get)
    monkeypatch.setattr(api.requests, "post", f.post)
    return f


def run(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        osiris(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


# ── search ────────────────────────────────────────────────────────────────────

def test_search_finds_courses_by_the_start_of_a_word(fake, capsys):
    code, out, err = run(["search", "epidem"], capsys)

    assert code == 0
    assert "INFOMEMD     Epidemiology and Medical Datascience\n" in out
    assert "7.5 EC  level M  Period 2  timeslot D" in out
    assert "Datasciencea" not in out                      # last year's stays out
    assert "2 courses in 2026-2027" in err
    assert fake.bodies[-1]["query"]["bool"]["must"][1]["multi_match"]["query"] == "EPIDEM"


def test_search_says_when_there_is_more(fake, capsys):
    code, out, err = run(["search", "epidem", "--limit", "1"], capsys)

    assert code == 0 and out.count("EC") == 1
    assert "1 of 2 courses in 2026-2027; --limit shows more" in err


def test_search_in_another_year(fake, capsys):
    code, out, _ = run(["search", "epidem", "--year", "2025"], capsys)

    assert code == 0 and "Datasciencea" in out and "INFOMEMDX" not in out


def test_search_as_json(fake, capsys):
    code, out, _ = run(["search", "statistics", "--json"], capsys)

    [c] = json.loads(out)
    assert code == 0
    assert c["code"] == "WISB263" and c["level"] == "2 (Bachelor Elaborating)"
    assert c["periods"] == ["Period 2 (16-11-2026 until 05-02-2027)"] and c["timeslots"] == ["D"]
    assert c["url"].endswith("cursuscode=WISB263&collegejaar=2026")


def test_search_filters_by_the_index_s_own_values(fake, capsys):
    code, out, _ = run(["search", "--level", "master", "--faculty", "science",
                        "--period", "2", "--timeslot", "a"], capsys)

    must = fake.bodies[-1]["query"]["bool"]["must"]
    assert code == 0
    assert {"terms": {"categorie_omschrijving": ["M (Master)"]}} in must
    assert {"terms": {"faculteit_naam": ["Faculty of Science"]}} in must
    assert {"nested": {"path": "blokken_nested", "query": {"terms": {
        "blokken_nested.periode_omschrijving": ["Period 2"]}}}} in must
    assert {"nested": {"path": "blokken_nested", "query": {"terms": {
        "blokken_nested.timeslots.timeslot": ["A (MON-morning, WED-afternoon)"]}}}} in must
    assert "INFOMEMD" in out and "WISB263" not in out


def test_a_filter_given_twice_takes_either_value(fake, capsys):
    code, out, _ = run(["search", "--level", "M", "--level", "2"], capsys)

    assert code == 0 and "INFOMEMD" in out and "WISB263" in out


@pytest.mark.parametrize("flag, error", [
    (["--level", "mast"], "matches several: M (Master); HM (Master Honours)"),
    (["--faculty", "biology"], "matches none of: Faculty of Science; Faculty of Social Sciences"),
])
def test_search_says_which_filter_values_there_are(fake, capsys, flag, error):
    code, _, err = run(["search", *flag], capsys)

    assert code == 2 and error in err


def test_search_by_lecturer_and_open_enrolment(fake, capsys):
    code, out, _ = run(["search", "--teacher", "STELLATO", "--open"], capsys)

    must = fake.bodies[-1]["query"]["bool"]["must"]
    assert code == 0 and "INFOMEMD" in out and "WISB263" not in out
    assert {"wildcard": {"docenten.docent": {"value": "*STELLATO*", "case_insensitive": True}}} in must
    assert {"range": {"inschrijfperiodes_cursus.datum_tm": {"gte": "now"}}} in must


def test_search_wants_something_to_search_for(fake, capsys):
    code, _, err = run(["search"], capsys)

    assert code == 2 and "Give a course code or name" in err and not fake.bodies


def test_search_with_nothing_found(fake, capsys):
    code, out, _ = run(["search", "astrology"], capsys)

    assert code == 0 and out == "No courses found in 2026-2027.\n"


@pytest.mark.parametrize("given, year", [("2026", "2026-2027"), ("2026-2027", "2026-2027"),
                                         ("2026/27", "2026-2027")])
def test_year(given, year):
    assert api.year(given) == year


@pytest.mark.parametrize("given", ["26", "2026-2028", "next"])
def test_a_wrong_year_is_refused(given, fake, capsys):
    code, _, err = run(["search", "x", "--year", given], capsys)

    assert code == 2 and "write it as 2026 or 2026-2027" in err


def test_osiris_errors_end_with_its_message(fake, capsys):
    fake.fail = "Er is een interne fout opgetreden in OSIRIS Link. Exchange-id: 1"

    code, _, err = run(["search", "x"], capsys)

    assert code == 1 and "ERROR: Osiris: Er is een interne fout" in err


def test_requests_carry_the_language(fake, monkeypatch):
    seen = []
    monkeypatch.setattr(api.requests, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        seen.append(headers["taal"]) or fake.post(url, json, headers))

    api.search(api.query("x", YEAR), lang="nl")

    assert seen == ["NL"]


# ── show ──────────────────────────────────────────────────────────────────────

def test_show_prints_the_course_page(fake, capsys):
    code, out, _ = run(["show", "infomemd"], capsys)

    assert code == 0
    assert out.startswith("INFOMEMD  Epidemiology and Medical Datascience  (2026-2027)\n"
                          "https://osiris-student.uu.nl/onderwijscatalogus/extern/cursus?cursuscode=INFOMEMD")
    assert "== Links ==\n  Schedule: https://mytimetable.uu.nl/link?timetable.key=INFOMEMD" in out
    assert "Credits: 7.5 EC\nOffered by: Utrecht University - Faculty of Science\n" in out
    assert "Course goals:\n  Students can:\n\n  • use mixed models\n  • impute & pool" in out
    assert "  - Course contact\n    dr. R.K. Stellato <r.k.stellato@uu.nl>" in out
    assert "OTHER" not in out and "Materials" not in out


def test_show_takes_the_exact_code(fake, capsys):
    """INFOMEMD also matches INFOMEMDX and last year's INFOMEMD; only this year's is shown."""
    code, out, _ = run(["show", "INFOMEMD", "--json"], capsys)

    page = json.loads(out)
    assert code == 0 and page["id"] == 249361 and page["year"] == YEAR
    assert [s["section"] for s in page["sections"]] == [
        "rubriek-links", "rubriek-praktische-info", "rubriek-inhoud", "rubriek-docenten"]


def test_show_an_unknown_course(fake, capsys):
    code, _, err = run(["show", "NOPE1"], capsys)

    assert code == 1 and "No course NOPE1 in 2026-2027's catalogue" in err


# ── filters ───────────────────────────────────────────────────────────────────

def test_filters_lists_each_flag_s_values(fake, capsys):
    code, out, _ = run(["filters"], capsys)

    assert code == 0
    assert "--level  (Category / level)\n  2 (Bachelor Elaborating)  [3]\n" in out
    assert "  D  [3]" in out
    period = fake.bodies[-1]["aggs"]["period"]["aggs"]["x"]["terms"]
    assert period["exclude"] == api.DATES_ONLY


def test_to_text_keeps_lines_and_drops_markup():
    html = ("<html><head><style>p {}</style></head><body><p>One&nbsp;&amp; two<br/>three</p>"
            "<ul><li>a</li><li>b</li></ul></body></html>")

    assert api.to_text(html) == "One & two\nthree\n\n• a\n• b"
    assert api.to_text(None) == "" and api.to_text(7.5) == "7.5"
