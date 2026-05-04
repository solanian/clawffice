FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend \
    CLAWFFICE_BACKEND_PORT=19000

WORKDIR /app

COPY backend/requirements.txt /tmp/clawffice-requirements.txt
RUN pip install --no-cache-dir -r /tmp/clawffice-requirements.txt

COPY . /app

EXPOSE 19000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % (os.getenv('CLAWFFICE_BACKEND_PORT') or os.getenv('STAR_BACKEND_PORT', '19000')), timeout=5).read()" || exit 1

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${CLAWFFICE_BACKEND_PORT:-${STAR_BACKEND_PORT:-19000}} --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-120} backend.app:app"]
