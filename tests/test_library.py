"""uu library: an article through UU's library in a window, or from Elsevier's API."""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from fake_uu import ARTICLE_PDF
from uu_cli.services.brightspace.cli import main as brightspace
from uu_cli.services.library import articles, elsevier, proxy
from uu_cli.services.library.cli import main as library

DOI = "10.1016/j.knosys.2025.113564"
PDF_NAME = "10.1016_j.knosys.2025.113564.pdf"


def run(main, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.mark.parametrize("ref, doi", [
    (DOI, DOI),
    (f"doi:{DOI}", DOI),
    (f"https://doi.org/{DOI}", DOI),
    ("https://link.springer.com/article/10.1007/s10462-023-10512-3?x=1", "10.1007/s10462-023-10512-3"),
    (f"https://doi.org/{DOI}.", DOI),
    ("https://link.springer.com/content/pdf/10.1007/978-3-319-75553-3.pdf", "10.1007/978-3-319-75553-3"),
    ("https://www.sciencedirect.com/science/article/pii/S0950705125006100", None),
])
def test_doi_of(ref, doi):
    assert articles.doi_of(ref) == doi


def test_the_proxy_link_picks_uu_and_carries_the_article():
    assert proxy.proxy_url(articles.article_url(DOI)) == (
        f"https://utrechtuniversity.idm.oclc.org/login?auth=uushibboleth&url=https://doi.org/{DOI}")


def test_the_same_pdf_twice_is_one_file(tmp_path):
    first = articles.save_pdf(tmp_path, b"%PDF one", DOI)
    assert articles.save_pdf(tmp_path, b"%PDF one", DOI) == first
    assert articles.save_pdf(tmp_path, b"%PDF two", DOI).name == "10.1016_j.knosys.2025.113564-2.pdf"


# ── uu library open: the library proxy in a window ─────────────────────────────

@pytest.mark.browser
def test_open_logs_in_at_the_library_and_saves_the_pdf_you_open(window, fake_uu, state, capsys):
    code, out, err = run(library, ["open", DOI, "-o", str(state / "papers")], capsys)

    assert code == 0, err
    assert (state / "papers" / PDF_NAME).read_bytes() == ARTICLE_PDF
    assert f"Saved: {state / 'papers' / PDF_NAME}" in out
    assert fake_uu.library_links == [f"auth=uushibboleth&url=https://doi.org/{DOI}"]
    assert fake_uu.library_logins == 1 and len(fake_uu.codes) == 1  # you logged in, in the window
    assert len(window) == 1 and not window[0].browser.is_connected()


@pytest.mark.browser
def test_a_pdf_with_its_own_doi_is_named_after_it(window, fake_uu, state, capsys):
    # A chapter page that also offers the whole book, as SpringerLink does.
    fake_uu.article_pdf = "/content/pdf/10.1007/978-3-319-75553-3.pdf"

    code, out, err = run(library, ["open", "10.1007/978-3-319-75553-3_9", "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / "10.1007_978-3-319-75553-3.pdf").read_bytes() == ARTICLE_PDF
    assert "its own DOI, 10.1007/978-3-319-75553-3" in err and "whole book" in err


@pytest.mark.browser
def test_open_needs_no_login_while_uus_session_lasts(window, fake_uu, state, capsys):
    run(brightspace, ["login", "--auth", "browser"], capsys)

    code, _, err = run(library, ["open", DOI, "-o", str(state / "papers")], capsys)

    assert code == 0, err
    assert len(fake_uu.codes) == 1  # only the Brightspace login asked for one
    assert fake_uu.library_logins == 1


@pytest.mark.browser
def test_the_library_session_is_kept_for_the_next_article(window, fake_uu, state, capsys):
    run(library, ["open", DOI, "-o", str(state / "papers")], capsys)

    code, _, err = run(library, ["open", DOI, "-o", str(state / "papers")], capsys)

    assert code == 0, err
    assert fake_uu.library_logins == 1  # the second run went straight to the article


@pytest.mark.browser
@pytest.mark.parametrize("window", [False], indirect=True)
def test_open_gives_up_without_a_pdf(window, fake_uu, state, monkeypatch, capsys):
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 1)

    code, _, err = run(library, ["open", DOI, "-o", str(state / "papers")], capsys)

    assert code == 1 and "No PDF saved" in err
    assert not (state / "papers").exists()
    assert len(window) == 1 and not window[0].browser.is_connected()


@pytest.mark.parametrize("argv", [["open", DOI], ["get", DOI], ["watch"]])
def test_the_window_needs_a_screen(argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    code, _, err = run(library, argv, capsys)

    assert code == 1 and f"`uu library {argv[0]}` needs a screen" in err


# ── uu library get: the window opens the PDF itself ────────────────────────────

@pytest.mark.browser
def test_get_opens_the_pdf_the_article_names_without_a_click(window, fake_uu, state, capsys):
    fake_uu.clicks_pdf = False

    code, out, err = run(library, ["get", DOI, "-o", str(state / "papers")], capsys)

    assert code == 0, err
    assert (state / "papers" / PDF_NAME).read_bytes() == ARTICLE_PDF
    assert f"Saved: {state / 'papers' / PDF_NAME}" in out
    assert fake_uu.library_logins == 1 and len(fake_uu.codes) == 1  # you only logged in
    assert len(window) == 1 and not window[0].browser.is_connected()


@pytest.mark.browser
def test_get_opens_a_publishers_own_pdf_address_through_the_proxy(window, fake_uu, state, capsys):
    chapter = "10.1007/978-3-319-75553-3_9"
    fake_uu.clicks_pdf = False
    fake_uu.citation_pdf_url = f"https://link.springer.com/content/pdf/{chapter}.pdf"

    code, _, err = run(library, ["get", chapter, "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / "10.1007_978-3-319-75553-3_9.pdf").read_bytes() == ARTICLE_PDF
    assert fake_uu.library_links[-1] == f"auth=uushibboleth&url={fake_uu.citation_pdf_url}"
    assert not any("springer.com" in url for url in fake_uu.elsewhere)  # never past the proxy


@pytest.mark.browser
def test_get_leaves_it_to_you_when_the_page_names_no_pdf(window, fake_uu, state, capsys):
    fake_uu.citation_pdf_url = None

    code, _, err = run(library, ["get", DOI, "-o", str(state)], capsys)

    assert code == 0, err  # you clicked "View PDF"
    assert (state / PDF_NAME).exists()
    assert "Opening the article's PDF" not in err


@pytest.mark.browser
def test_get_asks_you_to_take_over_when_the_pdf_does_not_come(window, fake_uu, state, monkeypatch,
                                                               capsys):
    fake_uu.clicks_pdf = False
    fake_uu.citation_pdf_url = "/article/gone.pdf"
    monkeypatch.setattr(proxy, "FETCH_WAIT_S", 0)
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 3)

    code, _, err = run(library, ["get", DOI, "-o", str(state)], capsys)

    assert code == 1 and "No PDF saved" in err
    assert "didn't come by itself: open or download it in the window" in err


# ── uu library watch: get, for every DOI an agent adds to a list ───────────────

CHAPTER = "10.1007/978-3-319-75553-3_9"


@pytest.fixture
def wanted(state, monkeypatch):
    """Write the watch list in state, as written long enough ago for watch to read it."""
    monkeypatch.setattr("uu_cli.services.library.cli.WATCH_POLL_S", 0.05)
    monkeypatch.setattr("uu_cli.services.library.cli.WATCH_PAUSE_S", 0)

    def write(*lines: str):
        path = state / "wanted.txt"
        path.write_text("".join(f"{line}\n" for line in lines))
        os.utime(path, (time.time() - 5, time.time() - 5))
    return write


@pytest.mark.browser
def test_watch_fetches_what_is_added_to_its_list_and_logs_it(window, fake_uu, state, wanted, capsys):
    fake_uu.clicks_pdf = False
    wanted("# for chapter 2", "see Thimm's paper", f"https://doi.org/{DOI}")

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert (state / PDF_NAME).read_bytes() == ARTICLE_PDF
    assert (state / "watch.log").read_text() == (
        f"see Thimm's paper\tnot a DOI\n{DOI}\t{(state / PDF_NAME).resolve()}\n")
    assert len(window) == 1 and not window[0].browser.is_connected()
    assert "the most for one run" in err


@pytest.mark.browser
def test_watch_skips_what_its_log_already_has(window, fake_uu, state, wanted, capsys):
    fake_uu.clicks_pdf = False
    (state / "watch.log").write_text(f"{DOI}\tno PDF\n")
    wanted(DOI.upper(), CHAPTER)

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert fake_uu.library_links == [f"auth=uushibboleth&url=https://doi.org/{CHAPTER}"]
    assert (state / "watch.log").read_text().splitlines()[-1].startswith(f"{CHAPTER}\t")


@pytest.mark.browser
def test_watch_opens_only_a_dois_own_link(window, fake_uu, state, wanted, capsys):
    fake_uu.clicks_pdf = False
    wanted(f"https://elsewhere.example/{DOI}?go=1")

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert fake_uu.library_links == [f"auth=uushibboleth&url=https://doi.org/{DOI}"]
    assert not any("elsewhere.example" in url for url in fake_uu.elsewhere)


@pytest.mark.browser
def test_watch_logs_a_window_without_a_pdf(window, fake_uu, state, wanted, monkeypatch, capsys):
    fake_uu.clicks_pdf = False
    fake_uu.citation_pdf_url = None
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 1)
    wanted(DOI)

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert (state / "watch.log").read_text() == f"{DOI}\tno PDF\n"


# ── uu library elsevier: Elsevier's API ────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    """Elsevier's API: answers maps a url prefix to (status, headers, body); calls lists requests."""
    api = SimpleNamespace(calls=[], answers={
        f"{elsevier.API}/entitlement/doi/": (200, {}, json.dumps(
            {"entitlement-response": {"document-entitlement": {"entitled": True}}}).encode()),
        f"{elsevier.API}/doi/": (200, {"content-type": "application/pdf"}, ARTICLE_PDF),
    })

    def get(url, headers):
        api.calls.append((url, headers))
        return next(answer for prefix, answer in api.answers.items() if url.startswith(prefix))

    monkeypatch.setattr(elsevier, "_get", get)
    monkeypatch.setattr("uu_cli.services.library.cli.PAUSE_S", 0)
    monkeypatch.setenv("ELSEVIER_API_KEY", "k3y")
    return api


def test_elsevier_saves_the_pdf(api, state, capsys):
    code, out, err = run(library, ["elsevier", f"https://doi.org/{DOI}", "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / PDF_NAME).read_bytes() == ARTICLE_PDF
    assert [url for url, _ in api.calls] == [f"{elsevier.API}/entitlement/doi/{DOI}",
                                             f"{elsevier.API}/doi/{DOI}"]
    assert all(headers["X-ELS-APIKey"] == "k3y" for _, headers in api.calls)


def test_elsevier_stops_when_you_have_no_access(api, state, capsys):
    api.answers[f"{elsevier.API}/entitlement/doi/"] = (200, {}, json.dumps(
        {"entitlement-response": {"document-entitlement": {"entitled": False}}}).encode())

    code, _, err = run(library, ["elsevier", DOI, "-o", str(state)], capsys)

    assert code == 1 and "eduroam" in err and "uu library open" in err
    assert len(api.calls) == 1  # no PDF requested
    assert not (state / PDF_NAME).exists()


def test_elsevier_follows_a_redirect_without_the_key(api, state, capsys):
    api.answers[f"{elsevier.API}/doi/"] = (303, {"location": "https://cdn.example/x.pdf"}, b"")
    api.answers["https://cdn.example/"] = (200, {"content-type": "application/pdf"}, ARTICLE_PDF)

    code, _, err = run(library, ["elsevier", DOI, "-o", str(state)], capsys)

    assert code == 0, err
    url, headers = api.calls[-1]
    assert url == "https://cdn.example/x.pdf" and "X-ELS-APIKey" not in headers


def test_elsevier_says_what_a_refusal_means(api, state, capsys):
    api.answers[f"{elsevier.API}/entitlement/doi/"] = (401, {}, b"")
    api.answers[f"{elsevier.API}/doi/"] = (401, {}, b"")

    code, _, err = run(library, ["elsevier", DOI, "-o", str(state)], capsys)

    assert code == 1 and "ELSEVIER_API_KEY" in err


def test_elsevier_needs_a_key(capsys):
    code, _, err = run(library, ["elsevier", DOI], capsys)

    assert code == 1 and "ELSEVIER_API_KEY" in err and "dev.elsevier.com" in err
