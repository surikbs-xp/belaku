FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=9000 \
    DB_PATH=/data/paper_trading.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Run unprivileged: this process holds live broker credentials and places real
# orders, so a compromised dependency should not also be root. /data is created
# and chowned here so a volume Docker initialises from this image inherits the
# right ownership.
#
# Note this cannot be a "start as root, chown, then drop privileges" entrypoint:
# compose sets cap_drop:ALL, which removes CAP_SETUID/CAP_SETGID (so root cannot
# switch user) *and* CAP_DAC_OVERRIDE (so root cannot write to a directory owned
# by another uid either). Being the right user from the start is the only option.
# See docs/running for upgrading a volume created by an older root-running image.
RUN useradd --create-home --uid 10001 belaku \
    && mkdir -p /data \
    && chown -R belaku:belaku /data /app
USER belaku

VOLUME ["/data"]
EXPOSE 9000

CMD ["python", "bot/bot.py"]
