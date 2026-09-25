# syntax=docker/dockerfile:1
# Official Playwright Python image: Chromium + all system libs preinstalled.
# PLAYWRIGHT_VERSION must match the base image tag so the pip package and the
# preinstalled browser build stay in lockstep (a mismatch makes Chromium refuse to start).
ARG PLAYWRIGHT_VERSION=1.58.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-jammy

WORKDIR /app
ARG PLAYWRIGHT_VERSION

# Python deps (pyotp/requests added; playwright pinned to the image's version).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "playwright==${PLAYWRIGHT_VERSION}"

COPY brightspace-cli.py ./

# Runtime state lives outside the image so the session/token can be bind-mounted.
ENV BRIGHTSPACE_SESSION_DIR=/data/session \
    BRIGHTSPACE_TOKEN_FILE=/data/token.json
RUN mkdir -p /data
VOLUME ["/data"]

# No credentials are baked in. Provide at runtime via env or flags, e.g.:
#   docker run --rm -it \
#     -e BRIGHTSPACE_SOLISID=... -e BRIGHTSPACE_PASSWORD=... -e BRIGHTSPACE_TOTP_SECRET=... \
#     -e BRIGHTSPACE_ORG_UNIT=... -v "$PWD/state:/data" \
#     uu-brightspace-cli sync --org-unit <OrgUnitId>
ENTRYPOINT ["python3", "brightspace-cli.py"]
CMD ["--help"]
