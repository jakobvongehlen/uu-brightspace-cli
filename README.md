# Brightspace CLI

Headless Brightspace (D2L) course-sync CLI (Playwright).

Built specifically for Utrecht University's Brightspace instance (https://uu.brightspace.com).

Log in with your SolisID + MFA (TOTP) and mirror the full contents of your enrolled
Brightspace courses - slides, PDFs, documents, assignment instructions, files linked
from inside pages, and schedules - to a local directory. You can also query your
courses, assignments, grades, and announcements from the command line.

## Features

- **Download an entire course** - `sync` walks every module and topic and mirrors the
  content to disk: file attachments (PDF, PPTX, DOCX, XLSX, …), inline content pages,
  and assignment instructions with the files they link to.
- **Sync every enrolled course** - `sync-all` skips group/team sections and supports
  `--dry-run` and `-o/--output`.
- **Follow linked files** - it also downloads files referenced from inside saved content
  pages (e.g. a lecture PDF embedded in a page), so you get the whole module, not just
  the top-level entries.
- **Schedules and deadlines** - detects Google Sheets schedules linked in course content,
  downloads them as CSV, and merges their deadlines with native Brightspace dropbox due dates.
- **Incremental** - files already on disk are skipped, so re-running a sync is cheap.
- **Headless authentication over MFA** - logs in non-interactively with SolisID + TOTP and
  caches the browser session and bearer token.
- **Course queries** - list enrolled courses, a course's table of contents, assignments
  (dropbox + LTI links), grades, and announcements.
- **Docker-ready** - build the bundled image and run it with your env and a mounted state volume.

## Requirements and install

Python 3.10+, pip, and Chromium with its system dependencies.

```sh
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Set environment variables in your shell before running the CLI.
No credentials are stored in the repo. Login caches browser state and a token
at the configured paths; keep these files outside the repository.

| Variable | Purpose / default |
| --- | --- |
| `BRIGHTSPACE_SOLISID` | Login username. |
| `BRIGHTSPACE_PASSWORD` | Login password. |
| `BRIGHTSPACE_TOTP_SECRET` | Secret used to generate MFA codes. |
| `BRIGHTSPACE_ORG_UNIT` | Course org unit for content commands; alternatively use `--org-unit`. |
| `BRIGHTSPACE_BASE_URL` | Brightspace server URL; defaults to `https://uu.brightspace.com`. |
| `BRIGHTSPACE_SESSION_DIR` | Persistent browser profile; defaults to `../brightspace/session` relative to the script directory. |
| `BRIGHTSPACE_TOKEN_FILE` | Cached token file; defaults to `../brightspace/token.json` relative to the script directory. |

## Usage

Single-course commands below (including `sync`) require the course's org-unit id, passed as `--org-unit <id>` or set via `BRIGHTSPACE_ORG_UNIT`; without it, `sync` exits with an error.

```sh
python3 brightspace-cli.py login                 # Authenticate and cache a token
python3 brightspace-cli.py courses               # List enrolled courses
python3 brightspace-cli.py toc                   # Show course table of contents
python3 brightspace-cli.py assignments           # List dropbox assignments and LTI links
python3 brightspace-cli.py grades                # Show grades
python3 brightspace-cli.py news                  # Show announcements
python3 brightspace-cli.py schedule -o ./output  # Download linked Google Sheets
python3 brightspace-cli.py deadlines             # Show upcoming deadlines
python3 brightspace-cli.py sync --org-unit <id> -o ./output  # Download ALL contents of ONE course (id required)
python3 brightspace-cli.py sync-all -o ./output  # Sync every enrolled course
python3 brightspace-cli.py sync-all --dry-run    # Preview courses without downloading
```

`sync` downloads the **entire** course, so you must tell it which one:
`python3 brightspace-cli.py sync --org-unit <id> -o ./output` (or set `BRIGHTSPACE_ORG_UNIT`).
It walks every module and topic and mirrors the content into `<output>/<org-unit>/`: file
attachments (PDF, PPTX, DOCX, XLSX, …), inline content pages, files linked from inside those
pages, assignment instructions, and any Google Sheets schedules. Re-run any time - files
already downloaded are skipped, so you can keep a local copy of the course up to date.

`sync-all` discovers enrolled course offerings automatically and uses the same output
layout as `sync`. It continues if a
course fails, prints a per-course summary, and exits nonzero if any course failed.

Run `python3 brightspace-cli.py --help` for global options, or append `--help`
to a subcommand for its options.

## Docker

The [Dockerfile](Dockerfile) uses the Playwright image with Chromium installed.
Build it, then pass environment variables from your shell and mount persistent
state at `/data`. The image sets the session and token paths shown below.

```sh
docker build -t brightspace-cli .
docker run --rm \
  -e BRIGHTSPACE_SOLISID \
  -e BRIGHTSPACE_PASSWORD \
  -e BRIGHTSPACE_TOTP_SECRET \
  -e BRIGHTSPACE_ORG_UNIT \
  -e BRIGHTSPACE_BASE_URL="${BRIGHTSPACE_BASE_URL:-https://uu.brightspace.com}" \
  -e BRIGHTSPACE_SESSION_DIR=/data/session \
  -e BRIGHTSPACE_TOKEN_FILE=/data/token.json \
  -v brightspace-state:/data \
  -v "$PWD/output:/output" \
  brightspace-cli sync -o /output
```

Replace `sync -o /output` with `login` to authenticate explicitly, or with
another subcommand. Reuse the state volume across runs.

## Disclaimer

This is an unofficial, community tool. It is NOT affiliated with, endorsed by,
or supported by Utrecht University, D2L / Brightspace, or any other institution.

It is provided AS IS, with no warranty. There is NO guarantee that it works,
keeps working, or produces correct results. Use at your own risk, and comply
with your institution's terms of use.
