# WebForge backend (API + agent + Chromium). The static front-end in web/ is also served from here.
# python:3.12-slim-bookworm satisfies pyproject's python>=3.11 (the Playwright base images ship Python 3.10).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WEBFORGE_CHROMIUM="" \
    WEBFORGE_DATA=/app/data \
    WEBFORGE_RUNS=/app/runs

WORKDIR /app

# Playwright's bundled Node runtime links libatomic, which slim images lack; it must exist before
# `playwright install --with-deps` can even start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Chromium + its system libraries come from Playwright itself, so the browser always matches the pip package.
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY webforge ./webforge
COPY bench ./bench
COPY fixtures ./fixtures
COPY web ./web

# Non-root runtime user; /app/data and /app/runs are writable by it (and seed named volumes with this ownership).
RUN useradd --create-home --uid 1000 webforge \
    && mkdir -p /app/data /app/runs \
    && chown -R webforge:webforge /app/data /app/runs
USER webforge

EXPOSE 8000
# `serve` listens on $PORT when set (Render/Fly/...), otherwise 8000.
CMD ["python", "-m", "webforge", "serve", "--host", "0.0.0.0"]
