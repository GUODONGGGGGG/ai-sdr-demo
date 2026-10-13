FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PUBLIC_DEMO=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 demo

COPY --chown=demo:demo server ./server
COPY --chown=demo:demo web ./web
COPY --chown=demo:demo data ./data
EXPOSE 80
# The CloudBase service probes port 80. Gunicorn binds it as root, then
# handles application requests with unprivileged demo workers.
CMD ["gunicorn", "--bind", "0.0.0.0:80", "--user", "demo", "--group", "demo", "--workers", "1", "--threads", "4", "--timeout", "120", "server.app:app"]
