"""bookings.uu.nl end to end, in a real Chromium, against the offline fake."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pyotp
import pytest

from fake_uu import PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth
from uu_cli.services.bookings import commands
from uu_cli.services.bookings.cli import main as bookings
from uu_cli.services.brightspace.cli import main as brightspace


def run(main, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def at_terminal(monkeypatch):
    """Someone at the terminal reading codes off their phone; returns the prompts they saw."""
    prompts: list[str] = []

    def answer(prompt=""):
        prompts.append(prompt)
        return SOLISID if prompt.startswith("Solis-id") else pyotp.TOTP(TOTP_SECRET).now()

    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)
    return prompts


WHEN = ["--date", "2026-10-25", "--from", "10:00", "--until", "12:00"]


@pytest.mark.browser
def test_search_logs_in_and_lists_the_free_spaces(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["search", "UBUSPseats", *WHEN], capsys)

    assert code == 0, err
    assert fake_uu.bookings_logins == 1 and len(fake_uu.codes) == 1
    assert out.splitlines() == [
        "  97448  3rd floor 3.20 102     single study space | 3rd floor",
        "  97740  6th floor 6.07 085     single study space | 6th floor"]
    q = fake_uu.bookings_searches[-1]
    assert (q["date"], q["start"], q["end"], q["gid"]) == ("2026-10-25", "10:00", "12:00", "4378")
    assert q["capacity"] == "-1"  # the type the page is for: its own default finds nothing


@pytest.mark.browser
def test_search_by_zone_name_and_filters(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["search", "UBUSPseats", *WHEN, "--zone", "6th", "--power",
                                    "--json"], capsys)

    assert code == 0, err
    assert json.loads(out)[0]["id"] == "97448"
    q = fake_uu.bookings_searches[-1]
    assert q["zone"] == "1884"


@pytest.mark.browser
def test_an_unknown_zone_names_the_choices(fake_uu, at_terminal, capsys):
    code, _, err = run(bookings, ["search", "UBUSPseats", "--zone", "basement"], capsys)

    assert code == 2 and "1881 (3rd floor)" in err and fake_uu.bookings_searches == []


@pytest.mark.browser
def test_the_code_is_asked_even_while_logged_in_at_uu(fake_uu, at_terminal, monkeypatch, capsys):
    run(brightspace, ["login"], capsys)

    code, _, err = run(bookings, ["login"], capsys)

    assert code == 0, err
    assert "wants a code too" in err and len(fake_uu.codes) == 2


@pytest.mark.browser
def test_the_session_is_kept_for_the_next_run(fake_uu, at_terminal, monkeypatch, capsys):
    run(bookings, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, out, err = run(bookings, ["search", "UBUSPseats", *WHEN], capsys)

    assert code == 0, err
    assert fake_uu.bookings_logins == 1 and "97448" in out


def test_book_needs_the_terms_accepted(capsys):
    code, out, err = run(bookings, ["book", "UBUSPseats", "97448", *WHEN], capsys)

    assert code == 2 and out == "" and "--accept-terms" in err


@pytest.mark.browser
def test_book_holds_the_space_and_submits_the_form(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["book", "UBUSPseats", "97740", *WHEN, "--accept-terms"], capsys)

    assert code == 0, err
    assert out == "Booked 6th floor 6.07 085, 2026-10-25 10:00-12:00.\n"
    assert "Booking Confirmed" in err and "uu bookings cancel" in err
    held = fake_uu.bookings_held[0]
    assert (held["seat_id"], held["start"], held["end"]) == ("97740", "2026-10-25 10:00:00",
                                                            "2026-10-25 12:00:00")
    sent = parse_qs(fake_uu.bookings_made[0]["body"])
    assert sent["terms"] == ["1"] and sent["session"] == ["50545853"]
    assert fake_uu.bookings_released == []


@pytest.mark.browser
def test_book_by_name(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["book", "UBUSPseats", "3rd floor 3.20 102", *WHEN,
                                    "--accept-terms"], capsys)

    assert code == 0, err
    assert fake_uu.bookings_held[0]["seat_id"] == "97448"


@pytest.mark.browser
def test_a_space_that_isnt_free_is_not_held(fake_uu, at_terminal, capsys):
    code, _, err = run(bookings, ["book", "UBUSPseats", "11111", *WHEN, "--accept-terms"], capsys)

    assert code == 1 and "isn't free then" in err
    assert fake_uu.bookings_held == []


@pytest.mark.browser
def test_a_refused_booking_gives_the_hold_back(fake_uu, at_terminal, capsys):
    fake_uu.bookings_checkout_fails = True

    code, out, err = run(bookings, ["book", "UBUSPseats", "97740", *WHEN, "--accept-terms"], capsys)

    assert code == 1 and out == ""
    assert "no longer available" in err
    assert fake_uu.bookings_made == []
    assert fake_uu.bookings_released == [{"eid": "14888", "id": "0", "session": "50545853"}]


@pytest.mark.browser
def test_a_broken_page_says_so(fake_uu, at_terminal, capsys):
    run(bookings, ["login"], capsys)

    code, _, err = run(bookings, ["search", "ash"], capsys)

    assert code == 1 and "broken on the site itself" in err


def test_places_needs_no_login(capsys):
    code, out, _ = run(bookings, ["places"], capsys)

    assert code == 0 and out.startswith("UBBseats ")


LINK = "https://bookings.uu.nl/equipment/cancel?id=6b27601a6048"


@pytest.mark.browser
def test_book_keeps_it_for_list(fake_uu, at_terminal, monkeypatch, capsys):
    run(bookings, ["book", "UBUSPseats", "97740", *WHEN, "--accept-terms"], capsys)
    monkeypatch.setattr(commands, "_now", lambda: "2026-10-24 12:00:00")

    code, out, err = run(bookings, ["list"], capsys)

    assert code == 0, err
    assert out == "2026-10-25 10:00-12:00  6th floor 6.07 085\n"


@pytest.mark.browser
def test_show_lists_a_confirmation(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["show", LINK], capsys)

    assert code == 0, err
    assert out == (" 45474228  4th floor 4.06 098     10:00 Sunday, October 25, 2026 - "
                   "11:00 Sunday, October 25, 2026\n")


@pytest.mark.browser
def test_cancel_cancels_what_the_confirmation_lists(fake_uu, at_terminal, capsys):
    code, out, err = run(bookings, ["cancel", LINK], capsys)

    assert code == 0, err
    assert out.startswith("Cancelled 4th floor 4.06 098, 10:00 Sunday, October 25, 2026")
    assert fake_uu.bookings_cancelled == ["45474228"]
    code, out, _ = run(bookings, ["list", "--all"], capsys)
    assert "confirmation 6b27601a6048  (cancelled)" in out


@pytest.mark.browser
def test_cancel_by_code_and_booking_id_and_nothing_left(fake_uu, at_terminal, capsys):
    code, _, err = run(bookings, ["cancel", "6b27601a6048", "--booking", "45474228"], capsys)
    assert code == 0, err

    code, _, err = run(bookings, ["cancel", "6b27601a6048"], capsys)

    assert code == 1 and "nothing left to cancel" in err


@pytest.mark.browser
def test_an_unknown_confirmation_says_so(fake_uu, at_terminal, capsys):
    code, _, err = run(bookings, ["show", "deadbeef0000"], capsys)

    assert code == 1 and "doesn't know confirmation deadbeef0000" in err


def test_a_link_that_isnt_a_cancel_link(capsys):
    code, _, err = run(bookings, ["cancel", "https://example.org/x?y=1"], capsys)

    assert code == 2 and "isn't a cancel link" in err
