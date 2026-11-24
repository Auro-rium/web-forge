# WebForge backend (API + agent + Chromium). The static front-end in web/ is also served from here.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEBFORGE_CHROMIUM="" \
    WEBFORGE_DATA=/app/data \
    WEBFORGE_RUNS=/app/runs

COPY requirements.txt .
# keep the playwright pip package in lockstep with the browsers baked into this image
RUN pip install -r requirements.txt "playwright==1.49.0"

COPY webforge ./webforge
COPY bench ./bench
COPY fixtures ./fixtures
COPY web ./web
RUN mkdir -p /app/data /app/runs

EXPOSE 8000
# Render/Fly/etc. inject $PORT; default 8000 locally
CMD ["sh", "-c", "uvicorn webforge.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
