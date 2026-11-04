"""remindo.uu.nl end to end, in a real Chromium, against the offline fake."""

from __future__ import annotations

import json

import pyotp
import pytest

from fake_uu import PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth
from uu_cli.services.remindo.cli import main as remindo


def run(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        remindo(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def at_terminal(monkeypatch):
    """Someone at the terminal reading codes off their phone."""
    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": SOLISID if prompt.startswith("Solis-id")
                        else pyotp.TOTP(TOTP_SECRET).now())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)


@pytest.mark.browser
def test_agenda_logs_in_through_the_broker_and_lists_what_is_coming(fake_uu, at_terminal, capsys):
    code, out, err = run(["agenda", "--json"], capsys)

    assert code == 0, err
    assert fake_uu.remindo_logins == 1 and len(fake_uu.codes) == 1
    practice, dss, dis = json.loads(out)
    assert practice == {
        "month": "October 2026", "day": "27 Oct – 08 Nov", "title": "INFOMDSS - Self Assessment",
        "type": "Practice", "time": "Oct 27, 2026, 11:59 PM – Nov 8, 2026, 11:59 PM", "location": "",
        "start": "https://remindo.uu.nl/dashboard/moment/start/75826"}
    assert (dss["day"], dss["type"], dss["location"]) == ("09 Nov", "Exam", "Chromebooks")
    assert dis["day"] == "09 Nov"  # a second exam that day shares the first one's header


@pytest.mark.browser
def test_the_session_is_kept_for_the_next_run(fake_uu, at_terminal, capsys):
    assert run(["login"], capsys)[0] == 0
    code, out, err = run(["agenda"], capsys)

    assert code == 0, err
    assert fake_uu.remindo_logins == 1 and len(fake_uu.codes) == 1
    assert "[20261109] INFOMDSS - Data science and society" in out


@pytest.mark.browser
def test_results_lists_every_type_and_says_which_can_be_viewed(fake_uu, at_terminal, capsys):
    code, out, err = run(["results"], capsys)

    assert code == 0, err
    assert "Exams\n  5177416 " in out and "(no details link)" in out
    assert "Graded practices\n  5167578 " in out
    assert "Practices" not in out  # no attempts there

    code, out, err = run(["results", "--type", "graded", "--json"], capsys)
    assert [r["id"] for r in json.loads(out)] == ["5167578"] and json.loads(out)[0]["viewable"]


@pytest.mark.browser
def test_result_shows_the_overview_and_each_question(fake_uu, at_terminal, capsys):
    code, out, err = run(["result", "5167578"], capsys)

    assert code == 0, err
    assert out.startswith("[practice exam] INFOME\nMethod engineering (INFOME) · graded practice · closed\n"
                          "2026-06-15 18:48:16 – 20:14:25\n")
    assert "    1  incorrect\n    2  mostly correct\n" in out

    code, out, err = run(["result", "5167578", "--questions"], capsys)
    assert code == 0, err
    assert ("Question 1 (incorrect)\n  A Customer object belongs to: "
            "[image: https://uu.remindotoetsmanager.nl/files/1/m.png]\n"
            "  →  M1: Model Layer\n   ✓ M0: Model Instance Layer  (1 pt.)\n") in out
    assert "  A meta-model is a model → True ✓\n  Class diagrams live at M0 → True  (correct: False)\n" in out


@pytest.mark.browser
def test_export_saves_text_json_and_images_a_folder_per_course(fake_uu, at_terminal, capsys, tmp_path):
    out_dir = tmp_path / "learning"
    code, out, err = run(["export", "5167578", "--out", str(out_dir)], capsys)

    assert code == 0, err
    folder = out_dir / "Method-engineering-INFOME"
    name = "practice-exam-INFOME-2026-06-15-5167578"
    assert f"5167578: 2 questions, 1 image → {folder / name}.txt" in out
    text = (folder / f"{name}.txt").read_text()
    assert text.startswith("[practice exam] INFOME\n")
    assert f"A Customer object belongs to: [image: {name}-images/q1-1.png]" in text
    assert (folder / f"{name}-images" / "q1-1.png").read_bytes().startswith(b"\x89PNG")
    saved = json.loads((folder / f"{name}.json").read_text())
    assert saved["questions"][0]["images"] == [
        {"url": "https://uu.remindotoetsmanager.nl/files/1/m.png", "file": f"{name}-images/q1-1.png"}]
    assert saved["questions"][1]["blocks"][0]["kind"] == "match"

    code, out, err = run(["export", "5167578", "--out", str(out_dir)], capsys)
    assert code == 0 and "already saved" in out


@pytest.mark.browser
def test_export_all_skips_the_results_you_may_not_view(fake_uu, at_terminal, capsys, tmp_path):
    code, out, err = run(["export", "--all", "--no-images", "--out", str(tmp_path / "all")], capsys)

    assert code == 0, err
    assert "5177416: skipped" in err and "5167578: 2 questions →" in out
    assert not list(tmp_path.glob("all/*/*-images"))


@pytest.mark.browser
def test_results_new_lists_only_what_is_new_or_changed(fake_uu, at_terminal, capsys):
    code, out, err = run(["results", "--new"], capsys)
    assert code == 0 and out == "" and "First run: remembered 2 results" in err

    code, out, err = run(["results", "--new"], capsys)
    assert code == 0 and out == ""  # nothing changed: quiet, for cron

    exam = fake_uu.remindo_results["exam"][0]
    exam["progress"] = "Grade: 7.5"
    fake_uu.remindo_results["practice"].append({**exam, "id": "5226862", "test": "INFOMDSS - Self Assessment"})
    code, out, err = run(["results", "--new"], capsys)
    assert code == 0, err
    assert ("Changed exam: 5177416  [20260623] INFOME - 4 - USP\n"
            "  progress: Questions answered: 30 → Grade: 7.5\n") in out
    assert "New practice: 5226862  INFOMDSS - Self Assessment\n" in out


@pytest.mark.browser
def test_a_result_you_may_not_view_says_so(fake_uu, at_terminal, capsys):
    code, out, err = run(["result", "5177416"], capsys)

    assert code == 1
    assert "viewing it may be disabled" in err


@pytest.mark.browser
def test_without_a_terminal_it_says_why_it_cannot_log_in(fake_uu, capsys):
    code, out, err = run(["agenda"], capsys)

    assert code == 1
    assert "Cannot log in" in err and fake_uu.remindo_logins == 0
