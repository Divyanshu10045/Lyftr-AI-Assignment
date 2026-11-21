# ---- stage 1: build wheels/deps --------------------------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---- stage 2: small runtime image -----------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/app.db \
    PORT=8000
COPY --from=builder /install /usr/local

RUN useradd --system --uid 10001 --home-dir /app appuser \
 && mkdir -p /app /data && chown -R appuser /app /data
WORKDIR /app
COPY --chown=appuser app ./app

USER appuser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"

# Shell form so Render/Railway/Fly can inject $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:get_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
