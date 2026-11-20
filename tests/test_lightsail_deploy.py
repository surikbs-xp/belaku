"""Tests for the Lightsail deployment helpers in ``deploy/lightsail``.

The scripts are run as programs by CI, not imported by the bot, so they are
loaded by path here. What is pinned is the part that is easy to get wrong and
expensive to discover in production: the environment map that replaces
``compose.yaml``, the guard that refuses to publish an open dashboard, and the
rollback spec.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy" / "lightsail"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, DEPLOY_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_spec = _load("lightsail_build_spec", "build-spec.py")
deploy = _load("lightsail_deploy", "deploy.py")


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "belaku.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_spec_carries_the_paths_compose_would_have_pinned(tmp_path) -> None:
    """Lightsail never reads compose.yaml, so the spec has to repeat it."""
    env_file = write_env(tmp_path, "DASHBOARD_PASSWORD=hunter2\n")
    out = tmp_path / "spec.json"

    assert build_spec.main(
        [
            "--service-name", "belaku",
            "--image", "belaku:abc",
            "--env-file", str(env_file),
            "--out", str(out),
        ]
    ) == 0

    spec = json.loads(out.read_text())
    env = spec["containers"]["bot"]["environment"]
    assert env["DB_PATH"] == "/data/paper_trading.db"
    assert env["GIFT_BOOK_DB_PATH"] == "/data/gift_book.db"
    assert env["PAPER_MIRROR_DB_PATH"] == "/data/paper_mirror.db"
    assert env["LOG_FILE"] == "/data/bot.log"
    assert env["HOLIDAY_CACHE_PATH"] == "/data/nse_holidays.json"
    assert env["WEBHOOK_HOST"] == "0.0.0.0"
    assert env["WEBHOOK_PORT"] == "9000"
    assert spec["publicEndpoint"]["containerPort"] == 9000
    assert spec["publicEndpoint"]["healthCheck"]["path"] == "/api/health"


def test_the_environment_file_overrides_the_defaults(tmp_path) -> None:
    env_file = write_env(
        tmp_path,
        "# a comment\n"
        "DASHBOARD_PASSWORD=hunter2\n"
        "EXECUTION_MODE=LIVE\n"
        "export STOP_LOSS_POINTS=100\n"
        'BOT_NAME="Belaku Test"\n',
    )
    out = tmp_path / "spec.json"

    build_spec.main(
        [
            "--service-name", "belaku",
            "--image", "belaku:abc",
            "--env-file", str(env_file),
            "--out", str(out),
        ]
    )

    env = json.loads(out.read_text())["containers"]["bot"]["environment"]
    assert env["EXECUTION_MODE"] == "LIVE"
    assert env["STOP_LOSS_POINTS"] == "100"
    assert env["BOT_NAME"] == "Belaku Test"


def test_paper_is_the_default_execution_mode(tmp_path) -> None:
    """A pipeline that deploys on every push must not arm LIVE by itself."""
    env_file = write_env(tmp_path, "DASHBOARD_PASSWORD=hunter2\n")
    out = tmp_path / "spec.json"

    build_spec.main(
        [
            "--service-name", "belaku",
            "--image", "belaku:abc",
            "--env-file", str(env_file),
            "--out", str(out),
        ]
    )

    assert json.loads(out.read_text())["containers"]["bot"]["environment"]["EXECUTION_MODE"] == "PAPER"


def test_an_empty_dashboard_password_is_refused(tmp_path, capsys) -> None:
    """The Lightsail endpoint is public; an open dashboard is not acceptable."""
    env_file = write_env(tmp_path, "KITE_API_KEY=key\n")
    out = tmp_path / "spec.json"

    assert build_spec.main(
        [
            "--service-name", "belaku",
            "--image", "belaku:abc",
            "--env-file", str(env_file),
            "--out", str(out),
        ]
    ) == 1
    assert not out.exists()
    assert "DASHBOARD_PASSWORD" in capsys.readouterr().err


def test_an_open_dashboard_can_be_forced_on_purpose(tmp_path) -> None:
    env_file = write_env(tmp_path, "ALLOW_OPEN_DASHBOARD=1\n")
    out = tmp_path / "spec.json"

    assert build_spec.main(
        [
            "--service-name", "belaku",
            "--image", "belaku:abc",
            "--env-file", str(env_file),
            "--out", str(out),
        ]
    ) == 0
    env = json.loads(out.read_text())["containers"]["bot"]["environment"]
    assert "ALLOW_OPEN_DASHBOARD" not in env


def test_the_health_check_accepts_a_host_rejection() -> None:
    """The platform probe sends no public Host header, so 400 must pass.

    TrustedHostMiddleware answers 400 to a request whose Host is not allowed,
    and the process is perfectly healthy when it does.
    """
    spec = build_spec.build(
        service_name="belaku",
        image="belaku:abc",
        container_name="bot",
        container_port=9000,
        env={},
    )
    assert spec["publicEndpoint"]["healthCheck"]["successCodes"] == "200-499"


def test_the_public_url_is_added_to_the_host_allow_list() -> None:
    spec = build_spec.build(
        service_name="belaku",
        image="belaku:abc",
        container_name="bot",
        container_port=9000,
        env={},
    )

    deploy.apply_url_env(spec, "https://belaku.abc123.ap-south-1.cs.amazonlightsail.com")

    env = spec["containers"]["bot"]["environment"]
    assert env["ALLOWED_HOSTS"].startswith("belaku.abc123.ap-south-1.cs.amazonlightsail.com,")
    assert env["KITE_REDIRECT_URL"] == (
        "https://belaku.abc123.ap-south-1.cs.amazonlightsail.com/zerodha/callback"
    )


def test_a_pinned_host_allow_list_is_not_overwritten() -> None:
    spec = build_spec.build(
        service_name="belaku",
        image="belaku:abc",
        container_name="bot",
        container_port=9000,
        env={"ALLOWED_HOSTS": "belaku.example.com"},
    )

    deploy.apply_url_env(spec, "https://belaku.abc123.ap-south-1.cs.amazonlightsail.com")

    assert spec["containers"]["bot"]["environment"]["ALLOWED_HOSTS"] == "belaku.example.com"


def test_the_rollback_spec_reuses_the_running_deployment() -> None:
    info = {
        "currentDeployment": {
            "state": "ACTIVE",
            "containers": {"bot": {"image": ":belaku.bot.6", "ports": {"9000": "HTTP"}}},
            "publicEndpoint": {"containerName": "bot", "containerPort": 9000},
        }
    }

    spec = deploy.previous_spec(info, "belaku")

    assert spec is not None
    assert spec["containers"]["bot"]["image"] == ":belaku.bot.6"
    assert spec["publicEndpoint"]["containerPort"] == 9000


def test_there_is_nothing_to_roll_back_to_on_a_first_deploy() -> None:
    assert deploy.previous_spec({"currentDeployment": {}}, "belaku") is None


def test_waiting_returns_once_the_pending_deployment_is_gone(monkeypatch) -> None:
    states = [
        {"nextDeployment": {"state": "ACTIVATING"}, "currentDeployment": {"state": "ACTIVE"}},
        {"currentDeployment": {"state": "ACTIVE"}, "url": "https://belaku.example"},
    ]
    monkeypatch.setattr(deploy, "service", lambda *_: states.pop(0))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    info = deploy.wait_for_deployment("belaku", "ap-south-1", timeout=30, interval=0)

    assert info["url"] == "https://belaku.example"


def test_waiting_ignores_the_previous_deployment_still_serving(monkeypatch) -> None:
    """Right after a submit the old deployment is still ACTIVE.

    Returning on it would report success before the new image had started, so
    the wait is pinned to a version higher than the one that was live.
    """
    states = [
        {"currentDeployment": {"state": "ACTIVE", "version": 6}},
        {"currentDeployment": {"state": "ACTIVE", "version": 7}, "url": "https://belaku.example"},
    ]
    monkeypatch.setattr(deploy, "service", lambda *_: states.pop(0))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    info = deploy.wait_for_deployment(
        "belaku", "ap-south-1", timeout=30, interval=0, after_version=6
    )

    assert info["url"] == "https://belaku.example"


def test_a_failed_deployment_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        deploy,
        "service",
        lambda *_: {"nextDeployment": {"state": "FAILED"}, "currentDeployment": {}},
    )
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    with pytest.raises(deploy.DeployError, match="FAILED"):
        deploy.wait_for_deployment("belaku", "ap-south-1", timeout=30, interval=0)


def test_the_service_lookup_uses_the_list_api(monkeypatch) -> None:
    """There is no ``get-container-service`` (singular) in the Lightsail API.

    The only read call is ``get-container-services``, which returns a list. A
    pipeline that calls the singular form fails on its first run with
    "argument operation: Invalid choice".
    """
    calls: list[tuple[str, ...]] = []

    def fake_aws(*args: str, capture: bool = True):
        calls.append(args)
        return {
            "containerServices": [
                {"containerServiceName": "other", "url": "https://other.example"},
                {"containerServiceName": "belaku", "url": "https://belaku.example"},
            ]
        }

    monkeypatch.setattr(deploy, "aws", fake_aws)

    info = deploy.service("belaku", "ap-south-1")

    assert info["url"] == "https://belaku.example"
    assert "get-container-services" in calls[0]


def test_a_missing_service_is_reported_as_not_found(monkeypatch) -> None:
    """``ensure_service`` creates the service only on ``NotFoundException``.

    The list API answers with an empty list rather than an error, so the
    absence has to be turned into that exception here or the first deploy
    would never create anything.
    """
    monkeypatch.setattr(deploy, "aws", lambda *a, **k: {"containerServices": []})

    with pytest.raises(deploy.DeployError, match="NotFoundException"):
        deploy.service("belaku", "ap-south-1")


def test_the_pushed_image_reference_is_scraped_from_the_plugin_output(monkeypatch) -> None:
    """``push-container-image`` prints text, not JSON.

    The CLI implements it as a customization that shells out to
    ``lightsailctl``; ``--output json`` is accepted and ignored. Parsing the
    output as JSON fails with "Expecting value: line 1 column 1".
    """
    stdout = (
        "Digest: sha256:abc123\n"
        'Image "belaku:abc123" registered.\n'
        'Refer to this image as ":belaku.bot.7" in deployments.\n'
    )

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self) -> None:
            self.stdout = stdout

    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: Result())

    assert deploy.push_image("belaku", "ap-south-1", "belaku:abc123", "bot") == ":belaku.bot.7"


def test_a_push_without_a_reference_is_an_error(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "something unexpected\n"
        stderr = ""

    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(deploy.DeployError, match="did not report an image reference"):
        deploy.push_image("belaku", "ap-south-1", "belaku:abc123", "bot")
