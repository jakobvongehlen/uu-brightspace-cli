"""uu library: an article through UU's library in a window, or from Elsevier's API."""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Error as PlaywrightError

from fake_uu import ARTICLE_IN_ISSUE, ARTICLE_PDF, EXPORTS, ISSUE_ZIP, LIBRARY, scopus_csv, zip_of
from uu_cli.services.brightspace.cli import main as brightspace
from uu_cli.services.library import articles, elsevier, handoff, proxy, scopus
from uu_cli.services.library.cli import main as library

DOI = "10.1016/j.knosys.2025.113564"
PDF_NAME = "10.1016_j.knosys.2025.113564.pdf"
NOT_IN_ISSUE = "10.1016/j.knosys.2025.113999"


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


def test_link_prints_the_library_link_without_a_screen(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    springer = "https://link.springer.com/article/10.1007/s10462-023-10512-3"

    code, out, err = run(library, ["link", DOI, springer], capsys)

    assert code == 0 and not err
    assert out.splitlines() == [
        f"https://utrechtuniversity.idm.oclc.org/login?auth=uushibboleth&url=https://doi.org/{DOI}",
        f"https://utrechtuniversity.idm.oclc.org/login?auth=uushibboleth&url={springer}",
    ]


def test_link_says_which_one_is_not_an_article(capsys):
    code, out, err = run(library, ["link", DOI, "not-a-doi"], capsys)

    assert code == 2 and "Not a DOI or a web address: not-a-doi" in err
    assert len(out.splitlines()) == 1  # the good one is still printed


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


@pytest.mark.browser
def test_a_download_that_is_not_a_pdf_is_kept_but_is_not_the_article(window, fake_uu, state,
                                                                       monkeypatch, capsys):
    # ScienceDirect's "Download full issue", for an article that isn't in that issue.
    fake_uu.clicks_issue = True
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 2)

    code, _, err = run(library, ["open", NOT_IN_ISSUE, "-o", str(state)], capsys)

    assert code == 1 and "No PDF saved" in err
    assert (state / "issue_12.zip").read_bytes() == ISSUE_ZIP
    assert "[LIBRARY] Download: issue 12.zip" in err
    assert f"Kept {state / 'issue_12.zip'}: it's a ZIP archive (" in err


@pytest.mark.browser
def test_the_article_is_taken_out_of_the_issue_zip(window, fake_uu, state, capsys):
    # ScienceDirect's "View PDF" was turned away by its bot check on 25-10-2026; the issue came.
    fake_uu.clicks_issue = True

    code, out, err = run(library, ["open", DOI, "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / PDF_NAME).read_bytes() == ARTICLE_IN_ISSUE
    assert f"Saved: {state / PDF_NAME}" in out
    assert f"[LIBRARY] Took {DOI} out of the ZIP (3 files)." in err
    assert not (state / "issue_12.zip").exists()


def test_a_zip_with_the_doi_twice_gives_no_article():
    twice = zip_of({"a.pdf": b"%PDF 10.1016/j.knosys.2025.113564", "b.pdf": b"%PDF 10.1016/j.knosys.2025.113564",
                    "notes.txt": b"10.1016/j.knosys.2025.113564"})

    assert proxy._article_in_zip(twice, DOI) == ([b"%PDF 10.1016/j.knosys.2025.113564"] * 2, 3)
    assert proxy._article_in_zip(b"PK\x03\x04 not a ZIP after all", DOI) == ([], 0)


@pytest.mark.browser
def test_the_window_says_when_a_bot_check_turns_it_away(window, fake_uu, state, monkeypatch, capsys):
    # ScienceDirect's "View PDF" and IEEE Xplore's article pages, through the proxy on 25-10-2026.
    fake_uu.clicks_pdf = False
    fake_uu.page_fetches = [f"{LIBRARY}/craft/cdn-cgi/verify/cr?ray=secret", f"{LIBRARY}/document/9364495"]
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 2)

    code, _, err = run(library, ["open", DOI, "-o", str(state)], capsys)

    assert code == 1, err
    assert "[LIBRARY] 403 from utrechtuniversity.idm.oclc.org/craft/cdn-cgi/verify/cr\n" in err
    assert "[LIBRARY] Cloudflare's bot check turned this window away. On ScienceDirect" in err
    assert "[LIBRARY] 418 from utrechtuniversity.idm.oclc.org/document/9364495\n" in err
    assert "[LIBRARY] The site turned UU's library proxy away (418)" in err
    assert "secret" not in err


@pytest.mark.browser
def test_a_site_that_turns_the_window_away_goes_to_your_own_browser(window, fake_uu, state, monkeypatch,
                                                                    capsys):
    # ScienceDirect on 29-10-2026: six "View PDF" clicks in the window got a 403, Firefox got the PDF.
    fake_uu.clicks_pdf = False
    fake_uu.page_fetches = [f"{LIBRARY}/craft/cdn-cgi/verify/cr?ray=secret"]
    pickup = state / "documents"
    pickup.mkdir()
    opened = []

    def you_save_it(link):
        opened.append(link)
        (pickup / "1-s2.0-main.pdf").write_bytes(b"%PDF-1.7 <prism:doi>" + DOI.encode() + b"</prism:doi>")
        return True

    monkeypatch.setattr(handoff, "_open", you_save_it)
    monkeypatch.setattr(handoff, "SETTLED_S", 0)
    monkeypatch.setattr(handoff, "PICKUP_POLL_S", 0.05)
    monkeypatch.setattr(handoff, "PICKUP_TIMEOUT_S", 10)

    code, out, err = run(library, ["open", DOI, "-o", str(state), "--pickup", str(pickup)], capsys)

    assert code == 0, err
    assert opened == [proxy.proxy_url(f"https://doi.org/{DOI}")]
    assert (state / PDF_NAME).read_bytes().startswith(b"%PDF-1.7 <prism:doi>")
    assert not (pickup / "1-s2.0-main.pdf").exists()  # moved, not copied
    assert f"Saved: {state / PDF_NAME}" in out
    assert "This site turns the window away, so your own browser opens it" in err
    assert len(window) == 1 and not window[0].browser.is_connected()  # closed before the hand-off


def test_the_pickup_takes_only_a_new_pdf_of_the_article(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(handoff, "SETTLED_S", 0)
    old = tmp_path / "old.pdf"
    old.write_bytes(b"%PDF " + DOI.encode())
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    since = time.time() - 1
    (tmp_path / "course-notes.pdf").write_bytes(b"%PDF nothing of the kind")
    (tmp_path / "page.pdf").write_bytes(b"<html>" + DOI.encode())  # a web page saved as .pdf
    said = set()

    assert handoff.pick_up(tmp_path, DOI, since, said) is None
    err = capsys.readouterr().err
    assert "course-notes.pdf came in, but it isn't the PDF of" in err and "page.pdf came in" in err
    assert "old.pdf" not in err

    article = tmp_path / "1-s2.0-main.pdf"
    article.write_bytes(b"%PDF " + DOI.upper().encode())
    monkeypatch.setattr(handoff, "SETTLED_S", 60)
    assert handoff.pick_up(tmp_path, DOI, since, said) is None  # the browser may still be writing it
    monkeypatch.setattr(handoff, "SETTLED_S", 0)
    assert handoff.pick_up(tmp_path, DOI, since, said) == article
    assert capsys.readouterr().err == ""  # each stranger is named once


@pytest.mark.browser
def test_the_window_says_where_it_goes_but_not_the_query(window, fake_uu, state, capsys):
    fake_uu.clicks_pdf = False

    code, _, err = run(library, ["get", DOI, "-o", str(state)], capsys)

    assert code == 0, err
    assert "[LIBRARY] Page: utrechtuniversity.idm.oclc.org/article\n" in err
    assert "auth=uushibboleth" not in err  # a query can hold a login ticket or a signed link


@pytest.mark.browser
def test_the_window_logs_your_click_and_what_came_of_it(window, fake_uu, state, capsys):
    # To learn per publisher which button gives the PDF, so the window can press it itself.
    fake_uu.article_pdf = "/article/main.pdf?md5=secret&pid=1-s2.0"

    code, _, err = run(library, ["open", DOI, "-o", str(state)], capsys)

    assert code == 0, err
    text = (state / proxy.CLICK_LOG).read_text(encoding="utf-8")
    log = [json.loads(line) for line in text.splitlines()]
    assert log[0]["doi"] == DOI and log[0]["page"] == "utrechtuniversity.idm.oclc.org/article"
    assert log[0]["click"] == {"tag": "a", "id": "pdf", "classes": "", "text": "View PDF",
                               "href": "utrechtuniversity.idm.oclc.org/article/main.pdf?md5&pid",
                               "by_you": False}  # the fake's script clicks; Chromium marks that
    assert {"method": "GET", "kind": "document", "status": 200, "type": "application/pdf",
            "url": "utrechtuniversity.idm.oclc.org/article/main.pdf?md5&pid"} in [e.get("response") for e in log]
    assert log[-1]["saved"] == PDF_NAME and log[-1]["from"] == "utrechtuniversity.idm.oclc.org/article/main.pdf?md5&pid"
    assert "[LIBRARY] Click: 'View PDF' on utrechtuniversity.idm.oclc.org/article\n" in err
    assert "secret" not in text and "1-s2.0" not in text


@pytest.mark.browser
def test_the_click_log_never_reads_what_you_typed(fake_uu):  # fake_uu: skipped without Chromium
    from playwright.sync_api import sync_playwright
    page_html = ('<input id="pw" type="password"><input id="send" type="submit" value="Send">'
                 '<button id="go" class="btn"><span>Get PDF</span></button>')
    seen = []
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        ctx = chromium.new_context()
        ctx.expose_binding("__uuClick", lambda source, info: seen.append(info))
        ctx.add_init_script(proxy.CLICK_SPY)
        page = ctx.new_page()
        page.route("https://publisher.test/", lambda route: route.fulfill(body=page_html, content_type="text/html"))
        page.goto("https://publisher.test/")
        page.fill("#pw", "hunter2")
        page.click("#pw")
        page.click("#send")
        page.click("#go span")
        page.wait_for_timeout(300)
        chromium.close()

    assert [(c["id"], c["text"], c["by_you"]) for c in seen] == [("send", "Send", True), ("go", "Get PDF", True)]
    assert "hunter2" not in json.dumps(seen)


@pytest.mark.browser
def test_no_click_log_without_a_click(window, fake_uu, state, capsys):
    fake_uu.clicks_pdf = False  # you only log in; `get` opens the PDF the page names

    code, _, err = run(library, ["get", DOI, "-o", str(state)], capsys)

    assert code == 0, err
    assert not (state / proxy.CLICK_LOG).exists()


@pytest.mark.browser
def test_the_window_says_what_the_page_could_not_fetch(window, fake_uu, state, monkeypatch, capsys):
    # ScienceDirect's "View PDF" spun without opening a tab (25-10-2026), and the window said nothing.
    fake_uu.clicks_pdf = False
    fake_uu.prepared_export = "q"  # served without the proxy's address allowed: the browser blocks it (CORS)
    fake_uu.page_fetches = ["https://pdf.example.org/link?token=secret", f"{LIBRARY}/gone?ticket=secret",
                            f"{EXPORTS}/export_1.csv"]
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 2)

    code, _, err = run(library, ["open", DOI, "-o", str(state)], capsys)

    assert code == 1, err
    assert "[LIBRARY] Failed: pdf.example.org/link: net::ERR_FAILED" in err
    assert "[LIBRARY] 404 from utrechtuniversity.idm.oclc.org/gone" in err
    assert ("[LIBRARY] The browser blocked a request: Access to fetch at "
            "'exports-scopus-com.utrechtuniversity.idm.oclc.org/export_1.csv'") in err
    assert "secret" not in err


@pytest.mark.browser
def test_an_article_link_that_stops_before_its_page_is_tried_once_more(window, fake_uu, state, wanted,
                                                                      capsys):
    # On 25-10-2026 such a stop ended a watch run at its 13th DOI.
    fake_uu.clicks_pdf = False
    fake_uu.login_aborts = 1
    wanted(DOI)

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert "[LIBRARY] The article's page didn't open (net::ERR_ABORTED): trying once more." in err
    assert (state / "watch.log").read_text() == f"{DOI}\t{(state / PDF_NAME).resolve()}\n"


def test_ctrl_c_is_not_hidden_by_the_window_closing(tmp_path, monkeypatch):
    # Ctrl+C stops Playwright's driver too, so closing the window fails as well; the Ctrl+C
    # must still come through, for `watch` to stop quietly.
    def interrupted(*_):
        raise KeyboardInterrupt

    def driver_gone(*_):
        raise Exception("Browser.close: Connection closed while reading from the driver")

    window = SimpleNamespace(on=lambda *_: None, expose_binding=lambda *_: None,
                             add_init_script=lambda *_: None, new_page=interrupted,
                             browser=SimpleNamespace(close=driver_gone))
    monkeypatch.setattr(proxy.browser, "open_window", lambda ctx: window)

    with pytest.raises(KeyboardInterrupt):
        proxy.open_article(None, f"https://doi.org/{DOI}", tmp_path, DOI)


@pytest.mark.parametrize("argv", [["open", DOI], ["get", DOI], ["watch"], ["scopus", "TITLE(x)"]])
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


# ── uu library watch: get, for every DOI added to a list ───────────────────────

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
    assert "[WATCH] 1/2 see Thimm's paper: not a DOI" in err
    assert f"[WATCH] 2/2 {DOI}" in err
    assert f"[WATCH] Done: 1 of 2 with a PDF; {state / 'watch.log'} has each result.\n" in err
    assert "the most for one run" not in err  # nothing was left over


@pytest.mark.browser
def test_watch_says_when_max_leaves_some_for_the_next_run(window, fake_uu, state, wanted, capsys):
    fake_uu.clicks_pdf = False
    wanted(DOI, CHAPTER)

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert f"[WATCH] 1/2 {DOI}" in err
    assert "the most for one run (--max): start it again for more" in err
    assert "Done" not in err


@pytest.mark.browser
def test_watch_shows_a_list_line_without_its_control_characters(window, fake_uu, state, wanted,
                                                                capsys):
    wanted("look up \x1b[31mthis\x1b[0m one", DOI)
    fake_uu.clicks_pdf = False

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert "[WATCH] 1/2 look up ?[31mthis?[0m one: not a DOI" in err
    assert "\x1b" not in err


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


@pytest.mark.browser
def test_watch_logs_what_it_kept_when_no_pdf_came(window, fake_uu, state, wanted, monkeypatch,
                                                  capsys):
    fake_uu.clicks_issue = True
    fake_uu.citation_pdf_url = None
    monkeypatch.setattr(proxy, "WINDOW_TIMEOUT_S", 2)
    wanted(NOT_IN_ISSUE)

    code, _, err = run(library, ["watch", "-o", str(state), "--max", "1"], capsys)

    assert code == 0, err
    assert (state / "watch.log").read_text() == (
        f"{NOT_IN_ISSUE}\tno PDF; kept {(state / 'issue_12.zip').resolve()}\n")


# ── uu library scopus: a search through the proxy, you export ─────────────────

QUERY = 'TITLE-ABS-KEY("defeasible rule*" AND aspic*)'


@pytest.fixture
def today():
    return f"{datetime.date.today():%Y-%m-%d}"


@pytest.mark.browser
def test_scopus_opens_the_query_through_the_library_and_saves_your_export(window, fake_uu, state, today,
                                                                          monkeypatch, capsys):
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 2)

    code, out, err = run(library, ["scopus", QUERY, "-o", str(state)], capsys)

    assert code == 0, err
    export = state / f"scopus-{today}.csv"
    assert export.read_bytes() == scopus_csv(QUERY)
    assert (state / "scopus.log").read_text() == f"{today}\t{QUERY}\t{export.name}\n"
    assert fake_uu.scopus_queries == [QUERY] and fake_uu.library_logins == 1
    assert f"Saved: {export}" in out
    assert f"[SCOPUS] 1/1: {QUERY}" in err and "[SCOPUS] Download: scopus.csv" in err
    assert "[SCOPUS] File: 200 text/csv, attachment from utrechtuniversity.idm.oclc.org/scopus/export\n" in err
    assert len(window) == 1 and not window[0].browser.is_connected()


@pytest.mark.browser
def test_scopus_runs_each_query_of_a_file_in_its_own_window(window, fake_uu, state, today, monkeypatch,
                                                            capsys):
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 2)
    queries = state / "queries.txt"
    queries.write_text(f"# gap 1\n{QUERY}\n\n# gap 3\nTITLE-ABS-KEY(enthymeme*)\n")

    code, _, err = run(library, ["scopus", "--queries", str(queries), "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / "scopus.log").read_text() == (f"{today}\t{QUERY}\tscopus-{today}.csv\n"
                                                  f"{today}\tTITLE-ABS-KEY(enthymeme*)\tscopus-{today}-2.csv\n")
    assert len(window) == 2 and fake_uu.library_logins == 1  # the second window keeps the login
    assert "[SCOPUS] Done: 2 file(s) for 2 queries" in err


@pytest.mark.browser
def test_scopus_saves_an_export_that_comes_through_the_save_dialog(window, fake_uu, state, today, monkeypatch,
                                                                  capsys):
    fake_uu.export_dialog = True
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 2)

    code, _, err = run(library, ["scopus", QUERY, "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / f"scopus-{today}.csv").read_bytes() == scopus_csv(QUERY)
    assert "[SCOPUS] save dialog for scopus.csv, saved as a download instead" in err
    assert "[SCOPUS] Download: scopus.csv" in err


class ExportsHost:
    """What Scopus's exports host answers the window: the file, allowed for Scopus's own address only."""
    status = 200
    headers = {"content-type": "binary/octet-stream", "access-control-allow-origin": "https://www.scopus.com"}

    def body(self):
        return scopus_csv(QUERY)


@pytest.mark.browser
def test_scopus_hands_the_page_its_export_with_its_own_address_allowed(window, fake_uu, state, today, monkeypatch,
                                                                      capsys):
    fake_uu.export_elsewhere = True
    monkeypatch.setattr(scopus, "_fetch_export", lambda route: ExportsHost())
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 2)

    code, _, err = run(library, ["scopus", QUERY, "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / f"scopus-{today}.csv").read_bytes() == scopus_csv(QUERY)
    assert "[SCOPUS] Download: scopus.csv" in err  # the page's own download, named by the page
    assert "Failed" not in err and "CORS" not in err and "tab of its own" not in err


@pytest.mark.browser
def test_scopus_opens_an_export_the_page_cannot_fetch_in_a_tab_of_its_own(window, fake_uu, state, today,
                                                                          monkeypatch, capsys):
    fake_uu.export_elsewhere = True

    def refused(route):
        raise PlaywrightError("Route.fetch: connect ECONNREFUSED")

    monkeypatch.setattr(scopus, "_fetch_export", refused)
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 3)

    code, _, err = run(library, ["scopus", QUERY, "-o", str(state)], capsys)

    assert code == 0, err
    assert (state / f"scopus-{today}.csv").read_bytes() == scopus_csv(QUERY)
    where = "exports-scopus-com.utrechtuniversity.idm.oclc.org/export_7ff0_2026-10-25T094554.csv"
    assert "[SCOPUS] Couldn't fetch the export: Route.fetch: connect ECONNREFUSED" in err
    assert f"[SCOPUS] Failed: {where}: net::ERR_FAILED" in err
    assert f"[SCOPUS] The browser blocked {where} (CORS)" in err
    assert "[SCOPUS] The page couldn't fetch its export; opening it in a tab of its own" in err
    assert (state / "scopus.log").read_text().count("\n") == 1  # opened once, saved once


@pytest.mark.browser
@pytest.mark.parametrize("window", [False], indirect=True)
def test_scopus_without_an_export_says_so(window, fake_uu, state, monkeypatch, capsys):
    monkeypatch.setattr(scopus, "WINDOW_TIMEOUT_S", 1)

    code, _, err = run(library, ["scopus", QUERY, "-o", str(state)], capsys)

    assert code == 1 and "No export saved for this query" in err
    assert not (state / "scopus.log").exists()


@pytest.mark.parametrize("argv", [["scopus"], ["scopus", QUERY, "--queries", "q.txt"]])
def test_scopus_wants_one_query_or_a_file(argv, monkeypatch, capsys):
    monkeypatch.setenv("DISPLAY", ":0")

    code, _, err = run(library, argv, capsys)

    assert code == 2 and "give one query, or --queries" in err


def test_the_scopus_address_carries_the_whole_query():
    url = scopus.results_url(QUERY)
    assert url.startswith("https://www.scopus.com/results/results.uri?") and "&" not in url.split("&s=")[1]
    assert parse_qs(urlparse(url).query)["s"] == [QUERY]


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
