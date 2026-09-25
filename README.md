# Brightspace CLI

Headless Brightspace (D2L) course-sync CLI (Playwright).

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

Content commands below use `BRIGHTSPACE_ORG_UNIT`.

```sh
python3 brightspace-cli.py login                 # Authenticate and cache a token
python3 brightspace-cli.py courses               # List enrolled courses
python3 brightspace-cli.py toc                   # Show course table of contents
python3 brightspace-cli.py assignments           # List dropbox assignments and LTI links
python3 brightspace-cli.py grades                # Show grades
python3 brightspace-cli.py news                  # Show announcements
python3 brightspace-cli.py schedule -o ./output  # Download linked Google Sheets
python3 brightspace-cli.py deadlines             # Show upcoming deadlines
python3 brightspace-cli.py sync -o ./output      # Sync course content
```

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
