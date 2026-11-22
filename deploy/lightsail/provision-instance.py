#!/usr/bin/env python3
"""Provision the Belaku Lightsail *instance* (not container service).

The container service has no persistent disk and no stable address, so every
deploy wipes ``/data`` (SQLite books, logs, the refreshed Kite token) and the
public URL changes with the service. An instance fixes both: a real block
device and a static IP.

This script is idempotent - it checks for each resource before creating it, so
re-running after a partial failure resumes rather than duplicating. It stops
short of installing anything on the box: the on-host runbook lives in
``website/docs/hosting.md``.

Every AWS call goes through the ``aws`` CLI, matching ``deploy.py``, so the
script stays dependency-free.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TERMINAL_STATES = {"running", "stopped"}


class ProvisionError(RuntimeError):
    pass


def aws(*args: str) -> dict:
    """Run an ``aws`` command and return parsed JSON."""
    cmd = ["aws", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProvisionError(f"{' '.join(cmd)} failed: {detail}")
    out = (result.stdout or "").strip()
    return json.loads(out) if out else {}


def aws_optional(*args: str) -> dict | None:
    """Run an ``aws`` command, returning None when the resource is absent.

    ``get-static-ip`` answers with ``NotFoundException`` for a name that does
    not exist, which is the normal "not created yet" case here rather than a
    failure.
    """
    try:
        return aws(*args)
    except ProvisionError as exc:
        if "NotFoundException" in str(exc):
            return None
        raise


def instance(name: str, region: str) -> dict | None:
    """Return the instance, or None when it does not exist yet."""
    payload = aws(
        "lightsail", "get-instances",
        "--region", region,
        "--output", "json",
    )
    for entry in payload.get("instances") or []:
        if entry.get("name") == name:
            return entry
    return None


def ensure_key_pair(name: str, public_key: Path, region: str) -> None:
    """Import the operator's SSH public key.

    Importing (rather than ``create-key-pair``) is what lets the operator keep
    using the key they already have; ``create-key-pair`` would generate a new
    private key that only exists in this process's output.
    """
    existing = aws(
        "lightsail", "get-key-pairs",
        "--region", region,
        "--output", "json",
    )
    for entry in existing.get("keyPairs") or []:
        if entry.get("name") == name:
            print(f"  key pair {name} already imported")
            return

    aws(
        "lightsail", "import-key-pair",
        "--key-pair-name", name,
        "--public-key-base64", _b64(public_key),
        "--region", region,
        "--output", "json",
    )
    print(f"  imported key pair {name} from {public_key}")


def _b64(path: Path) -> str:
    import base64

    return base64.b64encode(path.read_bytes()).decode("ascii")


def ensure_instance(
    name: str,
    region: str,
    zone: str,
    blueprint: str,
    bundle: str,
    key_pair: str,
) -> dict:
    """Create the instance if absent, then wait for it to be running."""
    found = instance(name, region)
    if found is None:
        print(f"  creating instance {name} ({bundle}, {blueprint}) in {zone}")
        aws(
            "lightsail", "create-instances",
            "--instance-names", name,
            "--availability-zone", zone,
            "--blueprint-id", blueprint,
            "--bundle-id", bundle,
            "--key-pair-name", key_pair,
            "--region", region,
            "--output", "json",
        )
    else:
        print(f"  instance {name} already exists ({found.get('state', {}).get('name')})")

    return wait_for_running(name, region)


def wait_for_running(name: str, region: str, timeout: float = 300.0) -> dict:
    """Poll until the instance reports ``running``.

    A freshly created instance is ``pending`` and has no public IP yet, so
    attaching a static IP before this returns fails with NotFoundException.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = instance(name, region)
        state = ((found or {}).get("state") or {}).get("name")
        if state in TERMINAL_STATES:
            return found or {}
        if time.monotonic() > deadline:
            raise ProvisionError(
                f"instance {name} did not reach running within {timeout:.0f}s "
                f"(last state: {state})"
            )
        time.sleep(5)


def ensure_static_ip(name: str, target: str, region: str) -> str:
    """Allocate a static IP and attach it, returning the address.

    Without this the instance's public IP changes on every stop/start, which
    would break the Kite redirect URL and the dashboard bookmark each time the
    box is cycled for cost.

    The *singular* ``get-static-ip`` is used deliberately: some organizations
    deny the plural ``GetStaticIps`` by service control policy in a region while
    leaving the singular call allowed, and the plural is only a convenience for
    listing. Asking for one name by name works either way.
    """
    existing = aws_optional(
        "lightsail", "get-static-ip",
        "--static-ip-name", name,
        "--region", region,
        "--output", "json",
    )
    if existing is not None:
        entry = existing["staticIp"]
        if entry.get("attachedTo") == target:
            print(f"  static IP {name} already attached ({entry.get('ipAddress')})")
            return entry["ipAddress"]
        print(f"  attaching existing static IP {name} to {target}")
        aws(
            "lightsail", "attach-static-ip",
            "--static-ip-name", name,
            "--instance-name", target,
            "--region", region,
            "--output", "json",
        )
        return entry["ipAddress"]

    print(f"  allocating static IP {name}")
    payload = aws(
        "lightsail", "allocate-static-ip",
        "--static-ip-name", name,
        "--region", region,
        "--output", "json",
    )
    address = payload["staticIp"]["ipAddress"]
    aws(
        "lightsail", "attach-static-ip",
        "--static-ip-name", name,
        "--instance-name", target,
        "--region", region,
        "--output", "json",
    )
    return address


def open_ssh_only(name: str, region: str) -> None:
    """Replace the default firewall with SSH-only.

    Lightsail opens port 80 by default. The bot needs no inbound ports at all -
    the dashboard is reached over an SSH tunnel - so anything beyond 22 is
    unnecessary exposure.
    """
    aws(
        "lightsail", "put-instance-public-ports",
        "--instance-name", name,
        "--port-infos", "fromPort=22,toPort=22,protocol=tcp",
        "--region", region,
        "--output", "json",
    )
    print("  firewall set to SSH (22/tcp) only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="belaku")
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--zone", default="ap-south-1a")
    parser.add_argument("--blueprint", default="ubuntu_24_04")
    parser.add_argument("--bundle", default="micro_3_2")
    parser.add_argument("--key-pair", default="belaku")
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--static-ip-name", default="belaku-ip")
    args = parser.parse_args(argv)

    if not args.public_key.is_file():
        print(f"error: public key not found: {args.public_key}", file=sys.stderr)
        return 2

    try:
        print(f"Provisioning {args.name} in {args.region}")
        ensure_key_pair(args.key_pair, args.public_key, args.region)
        ensure_instance(
            args.name, args.region, args.zone,
            args.blueprint, args.bundle, args.key_pair,
        )
        address = ensure_static_ip(args.static_ip_name, args.name, args.region)
        open_ssh_only(args.name, args.region)
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Instance ready: {address}")
    print(f"  ssh ubuntu@{address}")
    print()
    print("Next: follow website/docs/hosting.md from 'Install Docker'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
