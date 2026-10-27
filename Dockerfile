# Job Finder Hub. Built and run on the Pi with compose.yaml (see docs/DEPLOY.md).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HUB_DATA_DIR=/app/data \
    TZ=Europe/London

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Same uid as the Pi user, so the bind-mounted data folder stays editable from the host.
RUN useradd --uid 1000 --create-home hub && mkdir -p /app/data && chown hub:hub /app/data
USER hub

EXPOSE 80
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/login', timeout=4)"
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers"]
