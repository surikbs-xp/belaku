# syntax=docker/dockerfile:1
#
# Two stages: wheels are built in a throwaway stage, and the runtime image gets
# only the installed packages. A single-stage build left the compiler toolchain,
# pip's cache and every build-time header in the shipped image.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Build dependencies for the wheels that need compiling. They stay in this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --prefix installs into a tree that can be copied wholesale, so the runtime
# stage does not need pip to resolve anything.
#
# The trimming happens *here*, in the same layer as the install. Deleting files
# in a later stage does not shrink the image - it only hides them behind a whiteout,
# and the bytes stay in the layer that created them. Measured: trimming after the
# COPY saved 12 MB of a 413 MB image; trimming before it saves the real 60 MB.
RUN pip install --prefix=/install -r requirements.txt \
    && SITE=/install/lib/python3.12/site-packages \
    && rm -rf "$SITE"/pygments "$SITE"/pygments-* \
              "$SITE"/watchfiles "$SITE"/watchfiles-* \
              "$SITE"/pip "$SITE"/pip-* \
    && find "$SITE" -type d \( -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} + \
    && find "$SITE" -name '*.pyc' -delete \
    && find "$SITE" -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find "$SITE" -maxdepth 1 -name '*.dist-info' -type d \
         \( -name 'pygments*' -o -name 'watchfiles*' -o -name 'pip-*' \) -prune -exec rm -rf {} +


FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=9000 \
    DB_PATH=/data/paper_trading.db

WORKDIR /app

# Only the installed packages cross the stage boundary - no compiler, no headers,
# no pip cache.
COPY --from=builder /install /usr/local

# pip lives in the base image, not in the install tree, so it has to be removed
# here. Nothing installs into a running container, and it is 7 MB plus a set of
# vendored wheels.
RUN python -m pip --version >/dev/null 2>&1 \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
              /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
              /usr/local/lib/python3.12/site-packages/_distutils_hack \
    || true

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
