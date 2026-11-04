# syntax=docker/dockerfile:1
# Official Playwright Python image: Chromium + all system libs preinstalled.
# PLAYWRIGHT_VERSION must match the base image tag so the pip package and the
# preinstalled browser build stay in lockstep (a mismatch makes Chromium refuse to start).
ARG PLAYWRIGHT_VERSION=1.58.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-jammy

WORKDIR /app
ARG PLAYWRIGHT_VERSION

# The package (playwright pinned to the image's version). Editable, so state defaults
# resolve relative to /app exactly as they did for the single-file script.
COPY pyproject.toml README.md LICENSE brightspace-cli.py ./
COPY src ./src
RUN pip install --no-cache-dir -e . "playwright==${PLAYWRIGHT_VERSION}"

# Runtime state lives outside the image so the session/token can be bind-mounted.
ENV UU_STATE_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

# No credentials are baked in. Provide at runtime via env or flags, e.g.:
#   docker run --rm -it \
#     -e UU_SOLISID=... -e UU_PASSWORD=... -e UU_TOTP_SECRET=... \
#     -e UU_BRIGHTSPACE_ORG_UNIT=... -v "$PWD/state:/data" \
#     uu-brightspace-cli sync --org-unit <OrgUnitId>
ENTRYPOINT ["python3", "brightspace-cli.py"]
CMD ["--help"]
