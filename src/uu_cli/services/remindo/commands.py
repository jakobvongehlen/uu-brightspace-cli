"""Remindo commands: login, agenda, results, result, export, page."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PlaywrightError

from uu_cli import login, paths
from uu_cli.login import Site, ensure_login
from uu_cli.services.remindo import api
from uu_cli.services.remindo.api import SSO_URL, NotLoggedIn, RemindoError, logged_in

# How a question's status reads in the overview.
STATUS = {"correct": "correct", "mostly-correct": "mostly correct", "mostly-incorrect": "mostly incorrect",
          "incorrect": "incorrect", "unanswered": "unanswered", "review": "to be reviewed"}


SITE = Site("Remindo", SSO_URL, logged_in)


def _logged_in_call(ctx: BrowserContext, args, fn, *a):
    """fn(ctx, *a), logging in first when Remindo asks; None when the login can't happen."""
    try:
        return fn(ctx, *a)
    except NotLoggedIn:
        if not ensure_login(ctx, args, SITE):
            return None
        return fn(ctx, *a)


def cmd_login(ctx: BrowserContext, args) -> int:
    return login.cmd_login(ctx, args, SITE)


def cmd_agenda(ctx: BrowserContext, args) -> int:
    """Upcoming exams and practices, as the dashboard's agenda lists them."""
    items = _logged_in_call(ctx, args, api.agenda)
    if items is None:
        return 1
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("Nothing on your agenda.")
        return 0
    month = None
    for it in items:
        if it["month"] != month:
            month = it["month"]
            print(month)
        where = f"  ({it['location']})" if it["location"] else ""
        print(f"  {it['day']:<16} {it['type']:<9} {it['title']}")
        print(f"  {'':<16} {'':<9} {it['time']}{where}")
        if it["start"]:
            print(f"  {'':<16} {'':<9} Start: {it['start']}")
    return 0


def _all_results(ctx: BrowserContext, args, tabs) -> dict | None:
    """Your attempts per tab; None when the login can't happen."""
    found = {}
    for tab in tabs:
        got = _logged_in_call(ctx, args, api.results, tab)
        if got is None:
            return None
        rows, label = got
        found[tab] = rows
        shown, _, total = label.rpartition(" of ")
        if total.isdigit() and len(rows) < int(total):
            print(f"[REMINDO] {api.RESULT_TABS[tab]}: showing {len(rows)} of {total}.", file=sys.stderr)
    return found


def _seen_file() -> Path:
    """What `results --new` saw last time: next to the session, as the other state."""
    return paths.STATE_DIR / "remindo-seen.json"


def _new_results(found: dict) -> list[dict] | None:
    """Rows that are new or changed since the last --new, and remember these; None on the first run."""
    file = _seen_file()
    try:
        seen = json.loads(file.read_text())
    except (OSError, ValueError):
        seen = None
    now = {r["id"]: {k: r[k] for k in ("result", "status", "progress", "viewable")}
           for rows in found.values() for r in rows}
    paths.write_private(file, json.dumps(now, indent=2, ensure_ascii=False))
    if seen is None:
        return None
    changed = []
    for tab, rows in found.items():
        for r in rows:
            before = seen.get(r["id"])
            if before == now[r["id"]]:
                continue
            what = [] if before is None else [f"{k}: {before.get(k) or '–'} → {now[r['id']][k] or '–'}"
                                              for k in now[r["id"]] if before.get(k) != now[r["id"]][k]]
            changed.append({**r, "type": api.RESULT_TABS[tab], "new": before is None, "changes": what})
    return changed


def cmd_results(ctx: BrowserContext, args) -> int:
    """Your attempts per type (exams, graded practices, practices), newest first."""
    found = _all_results(ctx, args, list(api.RESULT_TABS) if args.type == "all" else [args.type])
    if found is None:
        return 1
    if args.new:
        changed = _new_results(found)
        if changed is None:
            print(f"[REMINDO] First run: remembered {sum(map(len, found.values()))} results; "
                  f"from now on --new lists what is new or changed.", file=sys.stderr)
            changed = []
        if args.json:
            print(json.dumps(changed, indent=2, ensure_ascii=False))
            return 0
        for r in changed:  # nothing at all when nothing changed, so cron stays quiet
            score = f"  {r['result']}" if r["result"] else ""
            print(f"{'New' if r['new'] else 'Changed'} {r['type']}: {r['id']}  {r['test']}{score}")
            for line in r["changes"]:
                print(f"  {line}")
        return 0
    if args.json:
        print(json.dumps(found if args.type == "all" else found[args.type], indent=2, ensure_ascii=False))
        return 0
    if not any(found.values()):
        print("No results.")
        return 0
    for tab, rows in found.items():
        if not rows:
            continue
        print(f"{api.RESULT_TABS[tab].capitalize()}s")
        for r in rows:
            view = "" if r["viewable"] else "  (no details link)"
            score = f"  {r['result']}" if r["result"] else ""
            print(f"  {r['id']:<9} {r['start']:<22} {r['test']}{score}{view}")
            if r["progress"]:
                print(f"  {'':<9} {'':<22} {r['progress']}")
        print()
    print("Details: uu remindo result <id> [--questions]")
    return 0


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _print_question(number, status: str, blocks: list[dict]) -> None:
    print(f"\nQuestion {number} ({STATUS.get(status, status)})")
    for b in blocks:
        if b.get("kind") == "text":
            print(_indent(b["text"], "  "))
            continue
        if b.get("prompt"):
            print(_indent(b["prompt"], "  "))
        if b["kind"] == "choice":
            for c in b["choices"]:
                mark = ("→" if c["yours"] else " ") + ("✓" if c["correct"] else " ")
                points = f"  ({c['points'].strip()})" if c["points"].strip() else ""
                lines = (c["text"] or "(empty)").splitlines()
                print(f"  {mark} {lines[0]}{points}")
                for line in lines[1:]:
                    print(f"     {line}")
        elif b["kind"] == "match":
            for p in b["pairs"]:
                yours, correct = ", ".join(p["yours"]) or "–", ", ".join(p["correct"]) or "–"
                source = p["source"].replace("\n", "; ")
                if yours == correct:
                    print(f"  {source} → {yours} ✓")
                else:
                    print(f"  {source} → {yours}  (correct: {correct})")
        else:
            print(_indent(b.get("text", ""), "  "))
            if b.get("yours"):
                print(f"  Your answer: {'; '.join(b['yours'])}")
            if b.get("correct"):
                print(f"  Correct: {'; '.join(b['correct'])}")


def cmd_result(ctx: BrowserContext, args) -> int:
    """One result: its overview, and with --questions every question with your answer."""
    try:
        res = _logged_in_call(ctx, args, api.result, args.id)
        if res is None:
            return 1
        if args.questions or args.json:
            for q in res["questions"]:
                q["blocks"] = api.question(ctx, q["id"]) if args.questions else None
    except RemindoError as err:
        print(f"ERROR: Remindo: {err}", file=sys.stderr)
        return 1
    if args.json:
        if not args.questions:
            for q in res["questions"]:
                q.pop("blocks", None)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    _print_result(res, args.questions)
    return 0


def _print_result(res: dict, questions: bool) -> None:
    print(f"{res['test']}")
    print(f"{res['course']} · {res['type']} · {res['status']}")
    print(f"{res['start']} – {res['end'][-8:] if res['end'][:10] == res['start'][:10] else res['end']}")
    if res["score"] is not None:
        print(f"Score: {res['score']:g}" + (f" / {res['max_score']:g}" if res["max_score"] else ""))
    if res["grade"]:
        print(f"Result: {res['grade']}")
    counts = dict(res["counts"])
    total = counts.pop("total", len(res["questions"]))
    counts.pop("answered", None)
    if counts:
        print(", ".join(f"{STATUS.get(k.replace('mostlyC', 'mostly-c').replace('mostlyI', 'mostly-i'), k)}: {v}"
                        for k, v in counts.items()) + f" (of {total})")
    if res["questions"] and len(res["questions"]) < total:
        print(f"Remindo shows {len(res['questions'])} of the {total} questions (the test is set up so).")
    if not res["questions"]:
        print("\nRemindo shows no questions for this result: viewing them is disabled.")
        return
    if not questions:
        for q in res["questions"]:
            print(f"  {q['number']:>3}  {STATUS.get(q['status'], q['status'])}")
        print("\nWith --questions: every question, → your answer, ✓ the correct one.")
        return
    print("\n→ your answer, ✓ the correct one")
    for q in res["questions"]:
        _print_question(q["number"], q["status"], q["blocks"])


def _slug(s: str) -> str:
    return re.sub(r"[^\w.]+", "-", s).strip("-") or "result"


def _map_strings(obj, fn):
    """obj with fn applied to every string in it (a question's blocks)."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    return obj


def _save_images(ctx: BrowserContext, res: dict, folder: Path, name: str) -> int:
    """Download each question's images next to the export, and point the text at the files."""
    saved = 0
    for q in res["questions"]:
        urls = []
        _map_strings(q["blocks"], lambda s: urls.extend(re.findall(r"\[image: (\S+?)\]", s)))
        files = {}
        for url in dict.fromkeys(urls):
            ext = Path(url.split("?")[0]).suffix.lower()
            rel = f"{name}-images/q{q['number']}-{len(files) + 1}{ext if len(ext) <= 5 else ''}"
            try:
                data = api.image(ctx, url)
            except (RemindoError, PlaywrightError) as err:
                print(f"[REMINDO] Question {q['number']}: image not saved ({err}).", file=sys.stderr)
                continue
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(data)
            files[url] = rel
            saved += 1
        if files:
            q["images"] = [{"url": u, "file": f} for u, f in files.items()]
            q["blocks"] = _map_strings(q["blocks"], lambda s: re.sub(
                r"\[image: (\S+?)\]", lambda m: f"[image: {files.get(m.group(1), m.group(1))}]", s))
    return saved


def _export(ctx: BrowserContext, args, result_id: str, out: Path) -> str:
    """Save one result as text and JSON (and its images); says what happened."""
    res = api.result(ctx, result_id)
    folder = out / _slug(res["course"])
    name = f"{_slug(res['test'])}-{res['start'][:10]}-{res['id']}"
    if (folder / f"{name}.json").exists() and not args.force:
        return f"{result_id}: already saved ({folder / name}.json)"
    for q in res["questions"]:
        q["blocks"] = api.question(ctx, q["id"])
    folder.mkdir(parents=True, exist_ok=True)
    images = _save_images(ctx, res, folder, name) if not args.no_images else 0
    text = io.StringIO()
    with contextlib.redirect_stdout(text):
        _print_result(res, True)
    (folder / f"{name}.txt").write_text(text.getvalue())
    (folder / f"{name}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n")
    extra = f", {images} image{'s' * (images != 1)}" if images else ""
    return f"{result_id}: {len(res['questions'])} questions{extra} → {folder / name}.txt"


def cmd_export(ctx: BrowserContext, args) -> int:
    """Save results as text and JSON, with their images: by id, or every result you may view."""
    out = Path(args.out).expanduser()
    ids = list(args.ids)
    if args.all:
        found = _all_results(ctx, args, list(api.RESULT_TABS))
        if found is None:
            return 1
        ids += [r["id"] for rows in found.values() for r in rows if r["id"] not in ids]
    if not ids:
        print("ERROR: give result ids (see `uu remindo results`) or --all.", file=sys.stderr)
        return 2
    failed = 0
    for result_id in ids:
        try:
            got = _logged_in_call(ctx, args, _export, args, result_id, out)
        except RemindoError as err:
            if args.all:  # most exams can't be viewed; that's expected, not an error
                print(f"{result_id}: skipped, Remindo shows no details", file=sys.stderr)
                continue
            print(f"ERROR: Remindo: {err}", file=sys.stderr)
            failed += 1
            continue
        if got is None:
            return 1
        print(got)
    return 1 if failed else 0


def cmd_page(ctx: BrowserContext, args) -> int:
    """One of Remindo's pages as text, with its links."""
    page = _logged_in_call(ctx, args, api.content, args.path)
    if page is None:
        return 1
    if args.json:
        print(json.dumps(page, indent=2, ensure_ascii=False))
        return 0
    print(f"# {page['title']}\n{page['url']}\n")
    print(page["text"].strip())
    if page["links"]:
        print("\nLinks:")
        for link in page["links"]:
            print(f"  {link['text']:<40} {link['href']}")
    return 0
