#!/usr/bin/env python3
"""Push a Belaku image to a Lightsail container service and deploy it.

Lightsail container services are deployed by *reference to an image stored on
the service*, not by tag in a registry. So the sequence is always:

1. ``push-container-image`` - upload the local image, get back a reference like
   ``:belaku.bot.7``.
2. ``create-container-service-deployment`` - submit a spec that names that
   reference.
3. Poll until the deployment is ``ACTIVE`` (or ``FAILED``).

If the new deployment fails, the previous one is re-submitted so the service is
left running the last known-good image rather than nothing at all.

Every AWS call goes through the ``aws`` CLI rather than boto3: the CLI is what
the Lightsail docs and the ``lightsailctl`` plugin are built around, and it
keeps this script dependency-free.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TERMINAL_STATES = {"ACTIVE", "FAILED", "INACTIVE"}


class DeployError(RuntimeError):
    pass


def aws(*args: str, capture: bool = True) -> Any:
    """Run an ``aws`` command, returning parsed JSON when asked to capture."""
    cmd = ["aws", *args]
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DeployError(f"{' '.join(cmd)} failed: {detail}")
    if not capture:
        return None
    out = (result.stdout or "").strip()
    return json.loads(out) if out else {}


def service(service_name: str, region: str) -> dict:
    """Describe one container service.

    There is no ``get-container-service`` (singular) in the Lightsail API - the
    only read call is ``get-container-services``, which returns a list and
    answers with an empty one for a name that does not exist. The missing
    service is reported as ``NotFoundException`` so ``ensure_service`` can tell
    "not created yet" apart from a real API failure.
    """
    payload = aws(
        "lightsail", "get-container-services",
        "--service-name", service_name,
        "--region", region,
        "--output", "json",
    )
    for entry in payload.get("containerServices") or []:
        if entry.get("containerServiceName") == service_name:
            return entry
    raise DeployError(f"NotFoundException: no container service named {service_name}")


def ensure_service(service_name: str, region: str, power: str, scale: int) -> dict:
    """Create the container service if it does not exist yet.

    Capacity is only set at creation time; changing it later is a separate
    ``update-container-service`` call, which this deliberately does not make -
    a deploy should not silently resize (and reprice) the service.
    """
    try:
        return service(service_name, region)
    except DeployError as exc:
        if "NotFoundException" not in str(exc):
            raise
    print(f"Creating container service {service_name} ({power}, scale {scale})")
    aws(
        "lightsail", "create-container-service",
        "--service-name", service_name,
        "--power", power,
        "--scale", str(scale),
        "--region", region,
        "--output", "json",
    )
    return wait_for_url(service_name, region)


def wait_for_url(service_name: str, region: str, timeout: int = 120) -> dict:
    """Poll until the service reports its public URL.

    A freshly created service can answer ``get-container-services`` before the
    domain is assigned. The URL is not cosmetic here: it becomes ALLOWED_HOSTS
    and KITE_REDIRECT_URL, and a deployment built without it would reject every
    request the browser makes.
    """
    deadline = time.monotonic() + timeout
    info: dict = {}
    while time.monotonic() < deadline:
        info = service(service_name, region)
        if info.get("url"):
            return info
        time.sleep(5)
    print("warning: the service has no public URL yet; ALLOWED_HOSTS will be incomplete", file=sys.stderr)
    return info


def push_image(service_name: str, region: str, image: str, label: str) -> str:
    """Upload ``image`` and return the reference to use in a deployment.

    ``push-container-image`` is not a modelled API call: the CLI implements it
    as a customization that shells out to the ``lightsailctl`` plugin, which
    prints human-readable text rather than JSON. ``--output json`` is accepted
    and ignored, so the reference has to be scraped from the line that says
    ``Refer to this image as "..." in deployments.``
    """
    cmd = [
        "aws", "lightsail", "push-container-image",
        "--service-name", service_name,
        "--label", label,
        "--image", image,
        "--region", region,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DeployError(f"{' '.join(cmd)} failed: {detail}")
    match = re.search(r'Refer to this image as "([^"]+)"', result.stdout or "")
    if not match:
        raise DeployError(
            "push-container-image did not report an image reference: "
            f"{(result.stdout or '').strip()}"
        )
    return match.group(1)


def submit(spec_path: Path, region: str) -> dict:
    payload = aws(
        "lightsail", "create-container-service-deployment",
        "--cli-input-json", f"file://{spec_path.resolve()}",
        "--region", region,
        "--output", "json",
    )
    return payload.get("containerService") or {}


def wait_for_deployment(
    service_name: str,
    region: str,
    timeout: int,
    interval: int,
    after_version: int | None = None,
) -> dict:
    """Poll until the pending deployment resolves.

    ``nextDeployment`` is the one being rolled out; ``currentDeployment`` is what
    is serving traffic right now. A deployment is done when ``nextDeployment``
    disappears and ``currentDeployment`` is ``ACTIVE``.

    ``after_version`` guards the window right after a submit, when the *previous*
    deployment is still the current one and still ``ACTIVE``. Without it the
    first poll would report success before the new image had started.
    """
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        info = service(service_name, region)
        pending = info.get("nextDeployment") or {}
        current = info.get("currentDeployment") or {}
        state = pending.get("state") or current.get("state") or info.get("state") or ""
        if state != last_state:
            print(f"  deployment state: {state}")
            last_state = state
        version = current.get("version")
        superseded = after_version is None or (
            isinstance(version, int) and version > after_version
        )
        if not pending and current.get("state") == "ACTIVE" and superseded:
            return info
        if state in TERMINAL_STATES and state != "ACTIVE":
            raise DeployError(f"deployment ended in state {state}")
        time.sleep(interval)
    raise DeployError(f"deployment did not become ACTIVE within {timeout}s")


def apply_url_env(spec: dict, url: str) -> None:
    """Add the settings that can only be known once the service exists.

    The public domain is assigned by Lightsail, so it cannot be baked into the
    spec file ahead of time. Both values are ``setdefault``: an operator who
    pinned them in the environment file keeps their value.
    """
    host = urlparse(url).hostname or ""
    if not host:
        return
    for container in spec.get("containers", {}).values():
        env = container.setdefault("environment", {})
        # The dashboard is served on the service's public HTTPS domain, so that
        # name has to be in the Host allow-list or TrustedHostMiddleware rejects
        # every request - including the browser's.
        env.setdefault("ALLOWED_HOSTS", f"{host},127.0.0.1,localhost,[::1]")
        # Kite sends the browser back here after login. Registering this exact
        # URL in the Kite console is what makes the daily token refresh work
        # without a tunnel.
        env.setdefault("KITE_REDIRECT_URL", f"{url.rstrip('/')}/zerodha/callback")


def previous_spec(info: dict, service_name: str) -> dict | None:
    """The spec that would restore the deployment currently serving traffic."""
    current = info.get("currentDeployment") or {}
    containers = current.get("containers")
    endpoint = current.get("publicEndpoint")
    if not containers or not endpoint:
        return None
    return {
        "serviceName": service_name,
        "containers": containers,
        "publicEndpoint": endpoint,
    }


def smoke_test(url: str, attempts: int, interval: int) -> None:
    """Confirm the public endpoint answers before calling the deploy a success.

    A deployment can be ``ACTIVE`` while the app is still warming up (the bot
    loads GIFT Nifty history on startup), so this retries rather than failing on
    the first connection error.
    """
    import urllib.error
    import urllib.request

    target = f"{url.rstrip('/')}/api/health"
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(target, timeout=10) as response:
                body = json.loads(response.read().decode())
            if body.get("ok"):
                print(f"  health check passed: {target}")
                return
            last = f"unexpected body: {body}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
        print(f"  health check {attempt}/{attempts} failed: {last}")
        if attempt < attempts:
            time.sleep(interval)
    raise DeployError(f"health check never passed at {target}: {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--image", required=True, help="Local image, e.g. belaku:abc1234")
    parser.add_argument("--label", default="bot", help="Image label stored on the service")
    parser.add_argument("--spec", type=Path, required=True, help="Deployment spec JSON")
    parser.add_argument("--power", default="micro")
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--smoke-attempts", type=int, default=12)
    parser.add_argument("--smoke-interval", type=int, default=10)
    parser.add_argument(
        "--no-rollback",
        action="store_true",
        help="Leave the failed deployment in place instead of restoring the previous one",
    )
    args = parser.parse_args(argv)

    info = ensure_service(args.service_name, args.region, args.power, args.scale)
    fallback = previous_spec(info, args.service_name)
    url = info.get("url") or ""

    print(f"Pushing {args.image} to {args.service_name}")
    reference = push_image(args.service_name, args.region, args.image, args.label)
    print(f"  stored as {reference}")

    # The spec is built before the push, so the image reference is substituted
    # here rather than passed in.
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    for container in spec.get("containers", {}).values():
        container["image"] = reference
    apply_url_env(spec, url)
    resolved = args.spec.with_suffix(".resolved.json")
    resolved.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    print("Creating deployment")
    # The version serving traffic now. The new deployment is only done once a
    # *higher* version is ACTIVE, otherwise the first poll would see the old
    # deployment still ACTIVE and report success before the new image started.
    baseline = (info.get("currentDeployment") or {}).get("version")
    try:
        submit(resolved, args.region)
        info = wait_for_deployment(
            args.service_name,
            args.region,
            args.timeout,
            args.interval,
            after_version=baseline if isinstance(baseline, int) else None,
        )
        url = info.get("url") or url
        if url:
            smoke_test(url, args.smoke_attempts, args.smoke_interval)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.no_rollback or fallback is None:
            print("No rollback performed.", file=sys.stderr)
            return 1
        print("Rolling back to the previous deployment", file=sys.stderr)
        rollback = args.spec.with_suffix(".rollback.json")
        rollback.write_text(json.dumps(fallback, indent=2) + "\n", encoding="utf-8")
        try:
            submit(rollback, args.region)
            wait_for_deployment(
                args.service_name,
                args.region,
                args.timeout,
                args.interval,
                after_version=baseline if isinstance(baseline, int) else None,
            )
            print("Rollback deployment is ACTIVE", file=sys.stderr)
        except DeployError as rollback_exc:
            print(f"error: rollback also failed: {rollback_exc}", file=sys.stderr)
        return 1

    print(f"Deployed. Dashboard: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
