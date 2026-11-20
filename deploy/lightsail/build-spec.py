#!/usr/bin/env python3
"""Build the ``create-container-service-deployment`` input for Belaku.

A Lightsail container service has no volumes and no secrets store, so the whole
runtime configuration has to be expressed in the deployment spec: the image
reference, the open port, and an environment map. This script assembles that
map from a ``KEY=VALUE`` file (written from GitHub secrets in CI) on top of the
defaults that ``compose.yaml`` would otherwise supply.

The output is the ``--cli-input-json`` document for
``aws lightsail create-container-service-deployment``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def defaults(port: int) -> dict[str, str]:
    """Values compose.yaml pins for the Docker deployment.

    They are repeated here because Lightsail never reads compose.yaml: without
    them the bot falls back to its in-package defaults and tries to write SQLite
    files into /app/bot.

    /data exists in the image (the Dockerfile creates and chowns it) but is
    *ephemeral* on Lightsail - it is recreated from the image on every
    deployment. See website/docs/lightsail.md.
    """
    return {
        "WEBHOOK_HOST": "0.0.0.0",
        "WEBHOOK_PORT": str(port),
        "DB_PATH": "/data/paper_trading.db",
        "GIFT_BOOK_DB_PATH": "/data/gift_book.db",
        "PAPER_MIRROR_DB_PATH": "/data/paper_mirror.db",
        "LOG_FILE": "/data/bot.log",
        "HOLIDAY_CACHE_PATH": "/data/nse_holidays.json",
        # Paper unless the operator says otherwise. Arming LIVE is a deliberate
        # act, and it should not be the default of a pipeline that deploys on
        # every push to main.
        "EXECUTION_MODE": "PAPER",
    }


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, as python-dotenv would."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file, ignoring comments and blank lines."""
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        env[key.strip()] = _unquote(value.strip())
    return env


def build(
    service_name: str,
    image: str,
    container_name: str,
    container_port: int,
    env: dict[str, str],
) -> dict:
    return {
        "serviceName": service_name,
        "containers": {
            container_name: {
                "image": image,
                "ports": {str(container_port): "HTTP"},
                "environment": env,
            }
        },
        "publicEndpoint": {
            "containerName": container_name,
            "containerPort": container_port,
            "healthCheck": {
                "path": "/api/health",
                # 200-499, not 200. The platform's health checker does not send
                # the service's public Host header, so TrustedHostMiddleware can
                # answer 400 to a perfectly healthy process. A 5xx still fails,
                # which is what the check is for: is the app serving?
                "successCodes": "200-499",
                "intervalSeconds": 30,
                "timeoutSeconds": 5,
                "healthyThreshold": 2,
                "unhealthyThreshold": 3,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--container-name", default="bot")
    parser.add_argument("--container-port", type=int, default=9000)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    env = defaults(args.container_port)
    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error(f"--env-file {args.env_file} does not exist")
        env.update(read_env_file(args.env_file))

    # The dashboard can arm LIVE trading and square off positions, and on
    # Lightsail it is reachable from the public internet. An unset password
    # would leave those endpoints open to anyone with the URL.
    if not env.get("DASHBOARD_PASSWORD"):
        print(
            "error: DASHBOARD_PASSWORD is empty. The Lightsail endpoint is public, "
            "so the dashboard must not be left open.\n"
            "       Set it in the environment file, or pass ALLOW_OPEN_DASHBOARD=1 "
            "if you really mean to expose an unauthenticated control API.",
            file=sys.stderr,
        )
        if env.get("ALLOW_OPEN_DASHBOARD") != "1":
            return 1
        env.pop("ALLOW_OPEN_DASHBOARD", None)

    for name in ("KITE_API_KEY", "KITE_API_SECRET"):
        if not env.get(name):
            print(f"warning: {name} is empty; the bot will start but cannot trade", file=sys.stderr)

    spec = build(
        service_name=args.service_name,
        image=args.image,
        container_name=args.container_name,
        container_port=args.container_port,
        env=env,
    )
    args.out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
