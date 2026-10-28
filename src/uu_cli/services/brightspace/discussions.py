"""A course's discussion forums: fetched with the bearer token, shown as threads, saved by sync."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext

from uu_cli.paths import safe_filename
from uu_cli.services.brightspace.api import BASE_URL, api_fetch, get_token

PAGE_SIZE = 1000  # the most the posts route gives per page

# What Brightspace puts above the parent post it copies into a reply.
QUOTE = re.compile(r"<<< Replied to post below >>>\n"
                   r"(?:Authored by: (?P<author>.*)\n)?(?:Authored on: (?P<date>.*)\n)?"
                   r"(?:Subject: .*\n)?")


def cmd_discussions(ctx: BrowserContext, args) -> int:
    """Print every forum, topic and thread of the course."""
    token = get_token(ctx, args)
    if not token:
        return 1
    forums = fetch_discussions(ctx, token, args.org_unit)
    if forums is None:
        print("Discussions fetch failed (no forums for this course?).", file=sys.stderr)
        return 1
    if not forums:
        print("No discussion forums.")
        return 0
    for forum in forums:
        print(render_forum_heading(forum))
        for topic in forum["Topics"]:
            print(render_topic(topic, topic["Posts"]))
    return 0


def save_discussions(ctx: BrowserContext, token: str, ou: str, out_dir: Path) -> int:
    """Write <out_dir>/_discussions/<forum>/<topic>.md and discussions.json; return the posts saved."""
    forums = fetch_discussions(ctx, token, ou)
    if not forums:
        print("[SYNC] No discussion forums.", file=sys.stderr)
        return 0
    disc_dir = out_dir / "_discussions"
    disc_dir.mkdir(parents=True, exist_ok=True)
    (disc_dir / "discussions.json").write_text(json.dumps(forums, indent=2, ensure_ascii=False))
    n_posts = 0
    for forum in forums:
        forum_dir = disc_dir / _folder(forum.get("Name"), forum.get("ForumId"))
        forum_dir.mkdir(parents=True, exist_ok=True)
        for topic in forum["Topics"]:
            # Rewritten every time: unlike files, threads grow.
            md = render_forum_heading(forum) + "\n" + render_topic(topic, topic["Posts"])
            (forum_dir / f"{_folder(topic.get('Name'), topic.get('TopicId'))}.md").write_text(md)
            n_posts += len(topic["Posts"])
    return n_posts


# ── fetching ──────────────────────────────────────────────────────────────────
def fetch_discussions(ctx: BrowserContext, token: str, ou: str) -> Optional[list[dict]]:
    """The course's forums, each with "Topics", each with "Posts"; None when there's no forum list."""
    base = f"{BASE_URL}/d2l/api/le/1.57/{ou}/discussions/forums/"
    forums = api_fetch(ctx, base, token)
    if not isinstance(forums, list):
        return None
    result = []
    for forum in forums:
        if not isinstance(forum, dict) or forum.get("ForumId") is None:
            continue
        topics = api_fetch(ctx, f"{base}{forum['ForumId']}/topics/", token)
        forum = {**forum, "Topics": []}
        for topic in topics if isinstance(topics, list) else []:
            if not isinstance(topic, dict) or topic.get("TopicId") is None:
                continue
            posts = fetch_posts(ctx, token, f"{base}{forum['ForumId']}/topics/{topic['TopicId']}/posts/")
            forum["Topics"].append({**topic, "Posts": posts})
        result.append(forum)
    return result


def fetch_posts(ctx: BrowserContext, token: str, url: str) -> list[dict]:
    """Every post of a topic. The route pages; past the last page it answers an error, not []."""
    posts: list[dict] = []
    seen: set = set()
    page = 1
    while True:
        batch = api_fetch(ctx, f"{url}?pageSize={PAGE_SIZE}&pageNumber={page}", token)
        if not isinstance(batch, list):
            break
        new = [p for p in batch if isinstance(p, dict) and p.get("PostId") not in seen]
        seen.update(p.get("PostId") for p in new)
        posts.extend(new)
        if len(batch) < PAGE_SIZE or not new:
            break
        page += 1
    return posts


# ── turning posts into text ───────────────────────────────────────────────────
class _HtmlText(HTMLParser):
    BLOCKS = {"p", "div", "pre", "blockquote", "table", "tr", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[list] = []  # [tag, items so far] per open <ul>/<ol>
        self.pre = 0
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            self.parts.append(f"\n{kind[1]}. " if kind[0] == "ol" else "\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self.BLOCKS:
            self.parts.append("\n\n")
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        if tag == "pre":
            self.pre += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n\n")
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """Readable plain text of a post's HTML: paragraphs, line breaks and lists kept."""
    parser = _HtmlText()
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [line.strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def post_body(post: dict) -> str:
    """The post's text. Message.Text is often empty; Message.Html then holds it."""
    msg = post.get("Message") or {}
    if not isinstance(msg, dict):
        return str(msg)
    return html_to_text(msg.get("Html") or "") or (msg.get("Text") or "").strip()


def split_quote(text: str) -> tuple[str, Optional[str], str]:
    """(reply, quoted author, quoted text) of a reply that copies its parent; quote parts empty if none."""
    m = QUOTE.search(text)
    if not m:
        return text, None, ""
    return text[:m.start()].strip(), (m.group("author") or "").strip() or None, text[m.end():].strip()


def _same(a: str, b: str) -> bool:
    """Same words: the copy in a reply has straight quotes where the post had curly ones."""
    words = lambda t: re.sub(r"\W+", "", t).casefold()
    return words(a) == words(b)


def thread(posts: list[dict]) -> list[tuple[int, dict]]:
    """(depth, post) in reading order: threads oldest first, each reply under the post it answers."""
    by_id = {p.get("PostId"): p for p in posts}
    children: dict = {}
    roots = []
    for p in posts:
        parent = p.get("ParentPostId")
        if parent is not None and parent in by_id and parent != p.get("PostId"):
            children.setdefault(parent, []).append(p)
        else:
            roots.append(p)  # a thread's first post, or a reply whose parent is gone
    order = lambda p: (p.get("DatePosted") or "", p.get("PostId") or 0)
    out: list[tuple[int, dict]] = []
    stack = [(0, p) for p in sorted(roots, key=order, reverse=True)]
    seen = set()
    while stack:
        depth, p = stack.pop()
        if id(p) in seen:
            continue
        seen.add(id(p))
        out.append((depth, p))
        stack.extend((depth + 1, c) for c in sorted(children.get(p.get("PostId"), []),
                                                    key=order, reverse=True))
    return out


def _when(date: Optional[str]) -> str:
    """An API timestamp (UTC) in local time, to the minute."""
    if not date:
        return ""
    try:
        return datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return date


def _subject(s: Optional[str]) -> str:
    return re.sub(r"^(?:re:\s*)+", "", (s or "").strip(), flags=re.I)


def render_post(post: dict, parent: Optional[dict] = None, thread_subject: str = "") -> str:
    """One post as Markdown: who and when, then the text. A copy of the parent is dropped; a quote
    that differs from it (a teacher answering inline) is kept as a quote."""
    head = f"**{post.get('PostingUserDisplayName') or 'Anonymous'}** · {_when(post.get('DatePosted'))}"
    if _subject(post.get("Subject")) and _subject(post.get("Subject")) != _subject(thread_subject):
        head += f" · {post['Subject']}"
    if post.get("IsDeleted"):
        head += " · (deleted)"
    if post.get("AttachmentCount"):
        head += f" · {post['AttachmentCount']} attachment(s), on Brightspace"
    reply, author, quoted = split_quote(post_body(post))
    parts = [head, reply] if reply else [head]
    if quoted and not (parent is not None and _same(quoted, post_body(parent))):
        parts.append(f"Quoting {author or 'the post above'}:\n\n"
                     + "\n".join(f"> {line}" if line else ">" for line in quoted.split("\n")))
    return "\n\n".join(parts)


def render_forum_heading(forum: dict) -> str:
    out = f"# {forum.get('Name') or 'Forum'}\n"
    desc = html_to_text((forum.get("Description") or {}).get("Html") or "")
    return out + (f"\n{desc}\n" if desc else "")


def render_topic(topic: dict, posts: list[dict]) -> str:
    """A topic as Markdown: a section per thread, replies nested as quotes."""
    out = [f"## {topic.get('Name') or 'Topic'}"]
    desc = html_to_text((topic.get("Description") or {}).get("Html") or "")
    if desc:
        out.append(desc)
    if not posts:
        out.append("(no posts)")
    by_id = {p.get("PostId"): p for p in posts}
    subject = ""
    for depth, post in thread(posts):
        parent = by_id.get(post.get("ParentPostId"))
        if depth == 0:
            subject = (post.get("Subject") or "").strip()
            out.append(f"### {subject or '(no subject)'}")
        text = render_post(post, parent, subject)
        prefix = "> " * depth
        out.append("\n".join((prefix + line).rstrip() for line in text.split("\n")))
    return "\n\n".join(out) + "\n"


def _folder(name: Optional[str], fallback) -> str:
    folder = safe_filename(name or "").replace("/", "_")
    return folder if folder not in ("", ".", "..", "untitled") else str(fallback)
