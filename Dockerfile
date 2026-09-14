# Tier 4 joins the existing three-tier compose stack. It needs no inbound
# traffic to do its job -- it polls the backend -- so the exposed port is for
# health checks and manual triggers only.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY pyproject.toml README.md ./
COPY tier4 ./tier4
RUN pip install --no-cache-dir -e .

COPY fixtures ./fixtures

# Config comes from the environment, never from a baked-in .env.
# Inside the compose network there is no TLS termination between containers, so
# plain http to the backend service name is correct here.
#
# TIER4_BE_EMAIL and TIER4_BE_PASSWORD are deliberately NOT set: they are
# required settings with no defaults, so the container fails fast if compose
# does not supply them, rather than starting up with a dev password.
ENV TIER4_BE_BASE_URL=http://dcast_t2_backend:8844 \
    TIER4_BE_VERIFY_TLS=false \
    TIER4_AUTOSTART_POLLER=true \
    TIER4_POLL_INTERVAL_SECONDS=10

EXPOSE 8901

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8901/health', timeout=4).status_code==200 else 1)"

# One process that both polls the queue and serves the operator endpoints.
# To split them, run this image twice: once with TIER4_AUTOSTART_POLLER=0 for
# the API, and once with the command overridden to `python -m tier4.cli poll`.
CMD ["python", "-m", "uvicorn", "tier4.app:app", "--host", "0.0.0.0", "--port", "8901"]
