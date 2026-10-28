"""Pieces that need no browser: tokens, saved credentials, schedules and filenames."""

from __future__ import annotations

import stat
import time
from argparse import Namespace

import pytest

from fake_uu import PASSWORD, SOLISID, TOTP_SECRET, make_jwt
from uu_cli import paths
from uu_cli.auth import credentials
from uu_cli.services.brightspace import api
from uu_cli.services.brightspace.discussions import html_to_text, render_topic, split_quote, thread
from uu_cli.services.brightspace.sheets import extract_google_sheets_urls, parse_sheet_deadlines
from uu_cli.services.brightspace.sync import course_dir, description_files


# ── tokens ────────────────────────────────────────────────────────────────────
def test_token_expired_reads_jwt_exp():
    assert not api.token_expired(make_jwt(time.time() + 3600))
    assert api.token_expired(make_jwt(time.time() - 1))
    assert api.token_expired(make_jwt(time.time() + 30))  # inside the 60 s margin


def test_token_expired_leaves_unreadable_tokens_to_the_api():
    assert not api.token_expired("not-a-jwt")


def test_saved_token_is_owner_only():
    api.save_token("abc")

    assert api.load_token()["token"] == "abc"
    assert stat.S_IMODE(paths.TOKEN_FILE.stat().st_mode) == 0o600


# ── encrypted credentials ─────────────────────────────────────────────────────
CREDS = {"solisid": SOLISID, "password": PASSWORD, "totp": TOTP_SECRET}


def test_credentials_round_trip(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_PASSPHRASE", "long enough passphrase")
    credentials.save_creds(CREDS, "long enough passphrase")

    assert credentials.load_creds() == CREDS
    assert stat.S_IMODE(paths.CREDS_FILE.stat().st_mode) == 0o600
    assert PASSWORD not in paths.CREDS_FILE.read_text()


def test_wrong_passphrase_exits(monkeypatch):
    credentials.save_creds(CREDS, "long enough passphrase")
    monkeypatch.setenv("BRIGHTSPACE_PASSPHRASE", "something else entirely")

    with pytest.raises(SystemExit, match="wrong passphrase"):
        credentials.load_creds()


def test_have_creds_fills_only_missing_fields(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_PASSPHRASE", "long enough passphrase")
    credentials.save_creds(CREDS, "long enough passphrase")
    args = Namespace(solisid="7654321", password="", totp="")

    assert credentials.have_creds(args)
    assert (args.solisid, args.password, args.totp) == ("7654321", PASSWORD, TOTP_SECRET)


def test_save_creds_rejects_a_secret_that_is_not_base32(monkeypatch, capsys):
    args = Namespace(solisid=SOLISID, password=PASSWORD, totp="not base32!")

    assert credentials.cmd_save_creds(args) == 1
    assert "not valid base32" in capsys.readouterr().err
    assert not paths.CREDS_FILE.exists()


# ── schedules ─────────────────────────────────────────────────────────────────
def test_extract_google_sheets_urls_gives_csv_export_links():
    html = '<a href="https://docs.google.com/spreadsheets/d/AbC-123_x/edit#gid=0">Schedule</a>'

    assert extract_google_sheets_urls(html) == [
        "https://docs.google.com/spreadsheets/d/AbC-123_x/export?format=csv&gid=0"]


def test_parse_sheet_deadlines(tmp_path):
    sheet = tmp_path / "schedule.csv"
    sheet.write_text(
        "Date,Topic,Deadline\n"
        "\"April 21, 2026Tue\",Intro,\n"
        "\"May 5, 2026Tue\",Lab,Report 1\n"
        "TBA,Later,\n")

    assert [(d["date"], d["title"]) for d in parse_sheet_deadlines(sheet)] == [
        ("2026-04-21", "Intro"), ("2026-05-05", "Report 1")]


# ── module descriptions ───────────────────────────────────────────────────────
def test_description_files_takes_course_file_quicklinks():
    html = ('<p><a href="https://uu.brightspace.com/d2l/common/dialogs/quickLink/quickLink.d2l'
            '?ou=1&amp;type=coursefile&amp;fileId=01.+Introduction26.pdf">Slides</a>'
            '<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&amp;type=coursefile'
            '&amp;fileId=week2%2Fnotes.pdf">Notes</a>'
            '<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&amp;type=quiz&amp;rcode=x">Quiz</a>'
            '<a href="https://example.org/paper.pdf">Paper</a></p>')

    assert description_files(html) == [
        ("https://uu.brightspace.com/d2l/common/dialogs/quickLink/quickLink.d2l"
         "?ou=1&type=coursefile&fileId=01.+Introduction26.pdf", "01. Introduction26.pdf"),
        ("https://uu.brightspace.com/d2l/common/dialogs/quickLink/quickLink.d2l"
         "?ou=1&type=coursefile&fileId=week2%2Fnotes.pdf", "notes.pdf"),
    ]


# ── course folders ────────────────────────────────────────────────────────────
def test_course_dir_is_named_after_the_course(tmp_path):
    assert course_dir(tmp_path, "4242", "2026-2027 1-GS Data/AI (INFOMX)") == \
        tmp_path / "2026-2027 1-GS Data_AI (INFOMX)"
    assert course_dir(tmp_path, "4242", None) == tmp_path / "4242"
    assert course_dir(tmp_path, "4242", " ") == tmp_path / "4242"


def test_course_dir_renames_the_org_unit_folder(tmp_path):
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "notes.txt").write_text("mine")

    named = course_dir(tmp_path, "4242", "Fake Course")

    assert (named / "notes.txt").read_text() == "mine"
    assert not (tmp_path / "4242").exists()


def test_course_dir_keeps_both_when_the_named_folder_exists(tmp_path):
    (tmp_path / "4242").mkdir()
    (tmp_path / "Fake Course").mkdir()

    assert course_dir(tmp_path, "4242", "Fake Course") == tmp_path / "Fake Course"
    assert (tmp_path / "4242").is_dir()


# ── discussions ───────────────────────────────────────────────────────────────
def post(pid, parent, date, html, who="Someone", subject="Question"):
    return {"PostId": pid, "ParentPostId": parent, "DatePosted": date, "Subject": subject,
            "PostingUserDisplayName": who, "Message": {"Text": "", "Html": html}}


def test_html_to_text_keeps_paragraphs_breaks_and_lists():
    html = ("<p>Dear all,<br><br>Two&nbsp;questions:</p>\n<ol><li>One?</li><li>Two?</li></ol>"
            "<pre><code>2.married.X</code></pre><p>&lt;A, r, B&gt; &amp; more</p>")

    assert html_to_text(html) == ("Dear all,\n\nTwo questions:\n\n1. One?\n2. Two?\n\n"
                                  "2.married.X\n\n<A, r, B> & more")


def test_split_quote_finds_the_copied_parent():
    text = html_to_text("<p>Thanks!</p><p>&lt;&lt;&lt; Replied to post below &gt;&gt;&gt;<br>"
                        "Authored by: A. Student<br>Authored on: 1 October 2026 10:00<br>"
                        "Subject: Q<br><br><br></p><p>Original question</p>")

    assert split_quote(text) == ("Thanks!", "A. Student", "Original question")
    assert split_quote("No quote here") == ("No quote here", None, "")


def test_thread_orders_threads_and_replies_oldest_first():
    posts = [  # newest first, as the API gives them
        post(4, 1, "2026-10-04T00:00:00Z", "<p>d</p>"),
        post(3, None, "2026-10-03T00:00:00Z", "<p>c</p>"),
        post(2, 1, "2026-10-02T00:00:00Z", "<p>b</p>"),
        post(5, 2, "2026-10-05T00:00:00Z", "<p>e</p>"),
        post(6, 99, "2026-10-06T00:00:00Z", "<p>orphan</p>"),
        post(1, None, "2026-10-01T00:00:00Z", "<p>a</p>"),
    ]

    assert [(d, p["PostId"]) for d, p in thread(posts)] == [
        (0, 1), (1, 2), (2, 5), (1, 4), (0, 3), (0, 6)]


def test_render_topic_drops_a_copied_parent_and_keeps_inline_answers():
    question = "<p>Is it \u201cdirected\u201d?</p><p>Can it loop?</p>"
    quote = ("&lt;&lt;&lt; Replied to post below &gt;&gt;&gt;<br>Authored by: A. Student<br>"
             "Authored on: 1 October 2026 10:00<br>Subject: Graphs<br><br>")
    posts = [
        post(1, None, "2026-10-01T08:00:00Z", question, "A. Student", "Graphs"),
        # A reply that copies the question (curly quotes made straight, as Brightspace does) ...
        post(2, 1, "2026-10-02T08:00:00Z", f'<p>See the slides.</p><p>{quote}</p>'
             '<p>Is it "directed"?</p><p>Can it loop?</p>', "Dr. Teacher", "Re: Graphs"),
        # ... and one that answers inside the copy.
        post(3, 1, "2026-10-03T08:00:00Z", f'<p>Answers below</p><p>{quote}</p><p>Is it "directed"?</p>'
             '<p><strong>No.</strong></p><p>Can it loop?</p>', "Dr. Teacher", "Graphs"),
    ]

    md = render_topic({"Name": "General"}, posts)

    assert md.startswith("## General\n\n### Graphs\n\n**A. Student** · 2026-10-01")
    assert "> See the slides." in md and md.count("Can it loop?") == 2
    assert "> Answers below\n>\n> Quoting A. Student:\n>\n> > Is it \"directed\"?\n> >\n> > No." in md
    assert "Re: Graphs" not in md  # a reply's subject only shows when it's another one


def test_render_topic_without_posts():
    assert render_topic({"Name": "Empty"}, []) == "## Empty\n\n(no posts)\n"


# ── filenames ─────────────────────────────────────────────────────────────────
def test_safe_filename():
    assert paths.safe_filename("a\\b\nc ") == "a_b c"
    assert paths.safe_filename("") == "untitled"
    assert len(paths.safe_filename("x" * 500)) == 180
