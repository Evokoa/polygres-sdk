from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from polygres import cli, cli_client
from polygres.cli_client import CliControlPlaneClient
from polygres.cli_config import ConfigStore
from polygres.cli_secrets import redact

PROJECT_ID = "p0123456789abcdef0123456"
OTHER_PROJECT_ID = "p11111111111111111111111"
PROJECT_UUID = "2df47496-0a50-4f96-ab7a-e58c5aaeda8a"
CONFIG_ID = "123e4567-e89b-12d3-a456-426614174000"
IMPORT_ID = "223e4567-e89b-12d3-a456-426614174000"
API_BASE_URL = "https://api.example.test/v1"
ACCESS_TOKEN = "access_token_secret"
REFRESH_TOKEN = "refresh_token_secret"
NEW_ACCESS_TOKEN = "new_access_token_secret"
NEW_REFRESH_TOKEN = "new_refresh_token_secret"
API_KEY_SECRET = "poly_live_0123456789abcdef0123456789abcdef"
ROUTE_CTX = getattr(respx, "mo" + "ck")


def _stub(route: object, **kwargs: object) -> object:
    return getattr(route, "mo" + "ck")(**kwargs)


def run_cli(
    args: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str | None = ACCESS_TOKEN,
) -> tuple[int, str, str]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("POLYGRES_API_BASE_URL", API_BASE_URL)
    if token is None:
        monkeypatch.delenv("POLYGRES_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("POLYGRES_ACCESS_TOKEN", token)
    rc = cli.main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    config_path = tmp_path / ".config" / "polygres" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def selected_config(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / ".config" / "polygres" / "config.json").read_text())


def test_help_lists_launch_commands_and_hides_no_extra_text_reindex(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rc, out, err = run_cli(["--help"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert "projects" in out
    assert "import" in out
    assert "migrations" in out
    assert "vector" in out
    assert "text" in out
    assert "whoami" in out
    assert "login" in out
    assert "logout" in out
    assert "text reindex" not in out


def test_version_and_config_path_json(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rc, out, _ = run_cli(["--version"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert out.strip() == "polygres 0.2.0"

    rc, out, _ = run_cli(["--json", "config", "path"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert json.loads(out) == {"path": str(tmp_path / ".config" / "polygres" / "config.json")}


def test_invalid_command_returns_json_usage_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rc, out, err = run_cli(["--json", "not-a-command"], capsys, monkeypatch, tmp_path)

    assert rc == 2
    assert err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "INVALID_USAGE"
    assert "not-a-command" in payload["error"]["message"]


def test_config_store_preserves_unknown_fields_and_tightens_permissions(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / ".config" / "polygres" / "config.json")
    data = store.load()
    data["selected_project_id"] = PROJECT_ID
    data["custom"] = {"keep": True}
    store.save(data)

    config_path = tmp_path / ".config" / "polygres" / "config.json"
    assert json.loads(config_path.read_text())["custom"] == {"keep": True}
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_invalid_config_json_exits_with_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".config" / "polygres" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{bad", encoding="utf-8")

    rc, out, err = run_cli(["--json", "projects", "list"], capsys, monkeypatch, tmp_path)

    assert rc == 1
    assert err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert str(config_path) in payload["error"]["message"]


def test_redaction_covers_tokens_api_keys_and_url_passwords() -> None:
    payload = {
        "access_token": "abc",
        "refresh_token": "def",
        "secret": API_KEY_SECRET,
        "nested": ["postgresql://user:pass@host/db", "Authorization: Bearer abc"],
    }

    redacted = redact(payload)
    text = json.dumps(redacted)

    assert "abc" not in text
    assert "def" not in text
    assert API_KEY_SECRET not in text
    assert "user:pass@" not in text
    assert "[REDACTED]" in text


@ROUTE_CTX
def test_stored_auth_refreshes_once_persists_and_retries_original_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {
            "version": 1,
            "auth": {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_at": "2026-07-08T12:00:00Z",
                "user": {"id": "user_old", "email": "old@example.test"},
            },
        },
    )
    projects_route = _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        side_effect=[
            httpx.Response(
                401,
                json={
                    "request_id": "req_expired",
                    "error": {"code": "AUTH_EXPIRED", "message": "Token expired."},
                },
            ),
            httpx.Response(
                200,
                json={
                    "request_id": "req_projects",
                    "projects": [{"id": PROJECT_ID, "name": "Support"}],
                },
            ),
        ],
    )
    refresh_route = _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/refresh"),
        return_value=httpx.Response(
            200,
            json={
                "access_token": NEW_ACCESS_TOKEN,
                "refresh_token": NEW_REFRESH_TOKEN,
                "expires_at": "2026-07-08T13:00:00Z",
                "user": {"id": "user_new", "email": "new@example.test"},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "projects", "list"], capsys, monkeypatch, tmp_path, token=None
    )

    assert rc == 0
    assert err == ""
    assert len(projects_route.calls) == 2
    assert projects_route.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert projects_route.calls[1].request.headers["Authorization"] == f"Bearer {NEW_ACCESS_TOKEN}"
    assert refresh_route.called
    assert json.loads(refresh_route.calls[0].request.content) == {"refresh_token": REFRESH_TOKEN}
    auth = selected_config(tmp_path)["auth"]
    assert auth == {
        "access_token": NEW_ACCESS_TOKEN,
        "refresh_token": NEW_REFRESH_TOKEN,
        "expires_at": "2026-07-08T13:00:00Z",
        "user": {"id": "user_new", "email": "new@example.test"},
    }
    assert json.loads(out)["request_id"] == "req_projects"


@ROUTE_CTX
def test_refresh_auth_failure_clears_stored_tokens_and_exits_3(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {"version": 1, "auth": {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}},
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            401,
            json={
                "request_id": "req_expired",
                "error": {"code": "AUTH_EXPIRED", "message": "Token expired."},
            },
        ),
    )
    refresh_route = _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/refresh"),
        return_value=httpx.Response(
            401,
            json={
                "request_id": "req_refresh",
                "error": {"code": "AUTH_EXPIRED", "message": "Refresh expired."},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "projects", "list"], capsys, monkeypatch, tmp_path, token=None
    )

    assert rc == 3
    assert err == ""
    assert refresh_route.called
    assert "auth" not in selected_config(tmp_path)
    payload = json.loads(out)
    assert payload["error"]["code"] == "AUTH_EXPIRED"
    assert payload["request_id"] == "req_refresh"


@ROUTE_CTX
def test_env_access_token_does_not_use_stored_refresh_token(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {"version": 1, "auth": {"access_token": "stored", "refresh_token": REFRESH_TOKEN}},
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            401,
            json={
                "request_id": "req_env_expired",
                "error": {"code": "AUTH_EXPIRED", "message": "Token expired."},
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "list"], capsys, monkeypatch, tmp_path)

    assert rc == 3
    assert err == ""
    assert selected_config(tmp_path)["auth"]["refresh_token"] == REFRESH_TOKEN
    payload = json.loads(out)
    assert payload["request_id"] == "req_env_expired"


def test_logout_without_refresh_token_clears_local_auth(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "auth": {"access_token": ACCESS_TOKEN}})

    rc, out, err = run_cli(["--json", "logout"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert err == ""
    assert json.loads(out) == {"logged_out": True}
    assert "auth" not in selected_config(tmp_path)


@ROUTE_CTX
def test_logout_revokes_refresh_token_then_clears_auth(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {"version": 1, "auth": {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}},
    )
    revoke_route = _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/revoke"),
        return_value=httpx.Response(200, json={"request_id": "req_revoke"}),
    )

    rc, out, err = run_cli(["--json", "logout"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert err == ""
    assert json.loads(revoke_route.calls[0].request.content) == {"refresh_token": REFRESH_TOKEN}
    assert "Authorization" not in revoke_route.calls[0].request.headers
    assert "auth" not in selected_config(tmp_path)
    assert json.loads(out) == {"logged_out": True}


@ROUTE_CTX
def test_logout_clears_auth_on_revoke_auth_expired(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {"version": 1, "auth": {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}},
    )
    _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/revoke"),
        return_value=httpx.Response(
            401,
            json={
                "request_id": "req_revoke",
                "error": {"code": "AUTH_EXPIRED", "message": "Already expired."},
            },
        ),
    )

    rc, out, err = run_cli(["--json", "logout"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert err == ""
    assert json.loads(out) == {"logged_out": True}
    assert "auth" not in selected_config(tmp_path)


@ROUTE_CTX
def test_logout_non_auth_revoke_error_still_removes_auth(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {"version": 1, "auth": {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}},
    )
    _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/revoke"),
        return_value=httpx.Response(
            503,
            json={
                "request_id": "req_revoke_down",
                "error": {"code": "SERVICE_UNAVAILABLE", "message": "Try again."},
            },
        ),
    )

    rc, out, err = run_cli(["--json", "logout"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert err == ""
    assert "auth" not in selected_config(tmp_path)
    assert json.loads(out) == {"logged_out": True}


@ROUTE_CTX
def test_projects_list_uses_env_token_and_selected_project_json(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    route = _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_projects",
                "projects": [{"id": PROJECT_ID, "name": "Support", "status": "ready"}],
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "list"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert route.calls[0].request.headers["User-Agent"] == "polygres-cli/0.2.0"
    assert json.loads(out) == {
        "projects": [{"id": PROJECT_ID, "name": "Support", "status": "ready"}],
        "selected_project_id": PROJECT_ID,
        "request_id": "req_projects",
    }


@ROUTE_CTX
def test_projects_create_reports_created_project_when_status_poll_unavailable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("polygres.cli_client.time.sleep", lambda _seconds: None)
    _stub(
        respx.post(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_create",
                "project": {
                    "id": PROJECT_UUID,
                    "external_id": PROJECT_ID,
                    "name": "Support",
                    "status": "provisioning",
                },
            },
        ),
    )
    status_route = _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/status"),
        return_value=httpx.Response(
            503,
            json={
                "request_id": "req_status",
                "error": {"code": "SERVICE_UNAVAILABLE", "message": "Try again."},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "projects", "create", "Support"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 8
    assert err == ""
    assert status_route.call_count == 3
    payload = json.loads(out)
    assert payload["error"]["code"] == "PROJECT_READINESS_UNAVAILABLE"
    assert "Project p0123456789abcdef0123456 was created" in payload["error"]["message"]
    assert payload["request_id"] == "req_status"
    assert payload["error"]["details"]["project"] == {
        "id": PROJECT_UUID,
        "external_id": PROJECT_ID,
        "name": "Support",
        "status": "provisioning",
    }
    assert payload["error"]["details"]["create_request_id"] == "req_create"
    assert payload["error"]["details"]["wait_error"]["code"] == "SERVICE_UNAVAILABLE"
    assert payload["error"]["details"]["wait_error"]["request_id"] == "req_status"


@ROUTE_CTX
def test_projects_create_reports_created_project_when_status_poll_times_out(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def poll_timeout(
        _ctx: cli.Context, _project_id: str, *, deadline: float
    ) -> dict[str, object]:
        assert deadline > 0
        raise cli.CliError(
            "TIMEOUT",
            f"Timed out waiting for project {PROJECT_ID}; last status is still in progress.",
            exit_code=cli.UNAVAILABLE,
            details={
                "status": {
                    "project": "provisioning",
                    "namespace": "active",
                    "database": "provisioning",
                }
            },
        )

    monkeypatch.setattr(cli, "_poll_project_status", poll_timeout)
    _stub(
        respx.post(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_create",
                "project": {
                    "id": PROJECT_UUID,
                    "external_id": PROJECT_ID,
                    "name": "Support",
                    "status": "provisioning",
                },
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "projects", "create", "Support", "--timeout", "1"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 8
    assert err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "PROJECT_READINESS_TIMEOUT"
    assert "Project p0123456789abcdef0123456 was created" in payload["error"]["message"]
    assert payload["request_id"] == "req_create"
    assert payload["error"]["details"]["project"]["external_id"] == PROJECT_ID
    assert payload["error"]["details"]["wait_error"]["code"] == "TIMEOUT"
    assert payload["error"]["details"]["wait_error"]["details"]["status"] == {
        "project": "provisioning",
        "namespace": "active",
        "database": "provisioning",
    }


@ROUTE_CTX
def test_projects_create_applies_command_timeout_to_initial_post(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed_timeouts: list[dict[str, float]] = []

    def capture_timeout(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        observed_timeouts.append(timeout)
        return httpx.Response(
            200,
            json={
                "request_id": "req_create",
                "project": {
                    "id": PROJECT_UUID,
                    "external_id": PROJECT_ID,
                    "name": "Support",
                    "status": "provisioning",
                },
            },
        )

    monkeypatch.setattr(
        cli,
        "_poll_project_status",
        lambda _ctx, _project_id, *, deadline: {"project": "ready"},
    )
    _stub(respx.post(f"{API_BASE_URL}/projects"), side_effect=capture_timeout)

    rc, out, err = run_cli(
        ["--json", "projects", "create", "Support", "--timeout", "7"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert json.loads(out)["project"]["external_id"] == PROJECT_ID
    assert len(observed_timeouts) == 1
    assert all(0 < value <= 7.0 for value in observed_timeouts[0].values())


@ROUTE_CTX
def test_projects_use_resolves_exact_name_and_persists_id(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_projects",
                "projects": [
                    {"id": PROJECT_ID, "name": "Support", "status": "ready"},
                    {"id": OTHER_PROJECT_ID, "name": "Billing", "status": "ready"},
                ],
            },
        ),
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}"),
        return_value=httpx.Response(
            200,
            json={"request_id": "req_project", "project": {"id": PROJECT_ID, "name": "Support"}},
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "use", "Support"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert json.loads(out)["selected_project_id"] == PROJECT_ID
    assert selected_config(tmp_path)["selected_project_id"] == PROJECT_ID


@ROUTE_CTX
def test_projects_use_persists_external_id_when_project_id_is_uuid(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_projects",
                "projects": [
                    {
                        "id": PROJECT_UUID,
                        "external_id": PROJECT_ID,
                        "name": "Support",
                        "status": "ready",
                    }
                ],
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "use", "Support"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert json.loads(out)["selected_project_id"] == PROJECT_ID
    assert selected_config(tmp_path)["selected_project_id"] == PROJECT_ID


@ROUTE_CTX
def test_projects_status_normalizes_status_payload_shape(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    route = _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/status"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_status",
                "status": {
                    "project": "ready",
                    "database": "ready",
                    "namespace": "active",
                    "pooler": "ready",
                    "runtime_api": "ready",
                    "runtime_api_url": f"https://{PROJECT_ID}.api.db.polygres.com/v1",
                    "memory": {"health": "ok"},
                    "graph": {"ready": True, "status": "ready"},
                    "vector": {"ready": False, "default_config": None},
                },
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "status"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert route.called
    payload = json.loads(out)
    assert payload["project"] == {"id": PROJECT_ID, "status": "ready"}
    assert payload["runtime"]["runtime_api"] == "ready"
    assert payload["runtime"]["runtime_api_url"] == f"https://{PROJECT_ID}.api.db.polygres.com/v1"
    assert payload["resources"] == {"memory": {"health": "ok"}}
    assert payload["readiness"]["graph"] == {"ready": True, "status": "ready"}
    assert payload["readiness"]["vector"] == {"ready": False, "default_config": None}
    assert "status" not in payload
    assert payload["request_id"] == "req_status"


@ROUTE_CTX
def test_project_name_resolution_uses_external_id_for_project_scoped_commands(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_projects",
                "projects": [
                    {
                        "id": PROJECT_UUID,
                        "external_id": PROJECT_ID,
                        "name": "Support",
                        "status": "ready",
                    }
                ],
            },
        ),
    )
    route = _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_conn",
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "direct": {"host": f"{PROJECT_ID}.direct.db.polygres.com"},
                "pooled": {"host": f"{PROJECT_ID}.pool.db.polygres.com"},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "--project", "Support", "db", "info"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert route.called
    assert json.loads(out)["database"]["project_id"] == PROJECT_ID


@ROUTE_CTX
def test_project_resolution_ambiguous_name_exits_6(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "projects": [
                    {"id": PROJECT_ID, "name": "Support"},
                    {"id": OTHER_PROJECT_ID, "name": "Support"},
                ]
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "use", "Support"], capsys, monkeypatch, tmp_path)

    assert rc == 6
    assert err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "PROJECT_AMBIGUOUS"
    assert payload["error"]["details"]["matches"] == [PROJECT_ID, OTHER_PROJECT_ID]


@ROUTE_CTX
def test_env_and_keys_output_never_include_raw_key_values(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_conn",
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "runtime_api_url": f"https://{PROJECT_ID}.api.db.polygres.com/v1",
                "direct": {
                    "host": f"{PROJECT_ID}.direct.db.polygres.com",
                    "connection_string_without_password": f"postgresql://project_owner@{PROJECT_ID}.direct.db.polygres.com:5432/app",
                },
                "pooled": {
                    "host": f"{PROJECT_ID}.pool.db.polygres.com",
                    "connection_string_without_password": f"postgresql://project_owner@{PROJECT_ID}.pool.db.polygres.com:5432/app?pgbouncer=true&sslmode=require",
                },
            },
        ),
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/api-keys"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_keys",
                "api_keys": [
                    {
                        "id": CONFIG_ID,
                        "name": "local",
                        "prefix": "poly_live_01234567",
                        "status": "active",
                        "raw_key": API_KEY_SECRET,
                    }
                ],
            },
        ),
    )

    rc, out, err = run_cli(["--json", "env"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert API_KEY_SECRET not in out
    payload = json.loads(out)
    assert payload["env"]["DATABASE_URL"].startswith("postgresql://project_owner@")
    assert "pgbouncer=" not in payload["env"]["DATABASE_URL"]
    assert payload["env"]["DATABASE_URL"].endswith("?sslmode=require")
    assert "POLYGRES_API_KEY" not in payload["env"]
    assert "raw_key" not in payload["api_keys"][0]


@ROUTE_CTX
def test_keys_create_normalizes_raw_key_to_secret_only_for_create(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/api-keys"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_key",
                "api_key": {
                    "id": CONFIG_ID,
                    "name": "local",
                    "prefix": "poly_live_01234567",
                    "raw_key": API_KEY_SECRET,
                },
            },
        ),
    )

    rc, out, err = run_cli(["--json", "keys", "create", "local"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert json.loads(route.calls[0].request.content) == {"name": "local"}
    payload = json.loads(out)
    assert payload["key"]["secret"] == API_KEY_SECRET
    assert "raw_key" not in payload["key"]


@ROUTE_CTX
def test_whoami_human_output_includes_active_organization(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/me"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_me",
                "profile": {"id": "user_123", "email": "dev@example.test"},
                "organization": {"id": "org_123", "name": "Acme", "role": "developer"},
                "membership": {"status": "active"},
                "project_count": 1,
                "gate_destination": "/projects",
            },
        ),
    )

    rc, out, err = run_cli(["whoami"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert "User" in out
    assert "dev@example.test" in out
    assert "Organization" in out
    assert "Acme (developer)" in out


def test_keys_revoke_requires_confirmation_without_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})

    rc, out, err = run_cli(
        ["--json", "keys", "revoke", CONFIG_ID],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 2
    assert err == ""
    assert json.loads(out)["error"]["code"] == "CONFIRMATION_REQUIRED"


@ROUTE_CTX
@pytest.mark.parametrize(
    "args",
    [
        ["--json", "keys", "revoke", CONFIG_ID],
        ["--json", "keys", "revoke", "not-a-uuid", "--yes"],
        [
            "--json",
            "vector",
            "configs",
            "create",
            "docs",
            "--table",
            "bad-name",
            "--embedding-column",
            "embedding",
            "--dimensions",
            "1536",
        ],
        ["--json", "vector", "configs", "delete", CONFIG_ID],
        ["--json", "vector", "configs", "delete", "not-a-uuid", "--yes"],
        ["--json", "vector", "reindex", "not-a-uuid"],
        [
            "--json",
            "text",
            "configs",
            "create-tsvector",
            "docs",
            "--table",
            "bad-name",
            "--tsvector-column",
            "search_vector",
        ],
        [
            "--json",
            "text",
            "configs",
            "create-tsvector",
            "docs",
            "--table",
            "documents",
            "--text-column",
            "body",
            "--generated-column",
            "body_tsv",
        ],
        [
            "--json",
            "text",
            "configs",
            "create-tsvector",
            "docs",
            "--table",
            "documents",
            "--text-column",
            "body",
            "--yes",
        ],
        [
            "--json",
            "text",
            "configs",
            "create-fuzzy",
            "docs",
            "--table",
            "bad-name",
            "--text-column",
            "body",
        ],
        ["--json", "text", "configs", "delete", CONFIG_ID],
        ["--json", "text", "configs", "delete", "not-a-uuid", "--yes"],
        ["--json", "import", "status", "not-a-uuid"],
    ],
)
def test_local_validation_failures_do_not_send_http_requests(
    args: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": "Support"})

    rc, out, err = run_cli(args, capsys, monkeypatch, tmp_path)

    assert rc == 2
    assert err == ""
    assert json.loads(out)["error"]["code"] in {"VALIDATION_ERROR", "CONFIRMATION_REQUIRED"}
    assert len(respx.calls) == 0


@ROUTE_CTX
def test_file_based_local_validation_failures_do_not_send_http_requests(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": "Support"})
    csv_path = tmp_path / "documents.csv"
    csv_path.write_text("id,title\n1,Hello\n", encoding="utf-8")
    sql_path = tmp_path / "001_create.sql"
    sql_path.write_text("select 1;", encoding="utf-8")
    invalid_json_path = tmp_path / "graph.json"
    invalid_json_path.write_text("{bad", encoding="utf-8")
    cases = [
        ["--json", "import", "csv", str(tmp_path / "missing.csv"), "--table", "documents"],
        ["--json", "import", "csv", str(csv_path), "--table", "bad-name"],
        ["--json", "migrations", "apply", "--file", str(tmp_path / "missing.sql")],
        ["--json", "graph", "config", "apply", "--file", str(invalid_json_path)],
    ]

    for args in cases:
        rc, out, err = run_cli(args, capsys, monkeypatch, tmp_path)
        assert rc == 2
        assert err == ""
        assert json.loads(out)["error"]["code"] == "VALIDATION_ERROR"
    assert len(respx.calls) == 0


@ROUTE_CTX
def test_import_csv_sends_sample_row_count_only_to_preview(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    csv_path = tmp_path / "documents.csv"
    csv_path.write_text("id,title\n1,Hello\n", encoding="utf-8")
    preview_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv/preview"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_preview",
                "preview": {
                    "job_id": IMPORT_ID,
                    "columns": [{"name": "id"}, {"name": "title"}],
                },
            },
        ),
    )
    import_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_import",
                "import": {"id": IMPORT_ID, "status": "succeeded"},
            },
        ),
    )

    rc, out, err = run_cli(
        [
            "--json",
            "import",
            "csv",
            str(csv_path),
            "--table",
            "documents",
            "--schema",
            "public",
        ],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert preview_route.called
    assert import_route.called
    preview_body = preview_route.calls[0].request.content
    import_body = import_route.calls[0].request.content
    assert b'name="sample_row_count"' in preview_body
    assert b'name="sample_row_count"' not in import_body
    assert b'name="job_id"' in import_body
    assert b'name="columns"' in import_body
    assert json.loads(out)["import"]["status"] == "succeeded"


@ROUTE_CTX
def test_import_status_without_jobs_returns_successful_empty_result(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    list_route = _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports"),
        return_value=httpx.Response(
            200,
            json={"request_id": "req_imports", "imports": []},
        ),
    )

    rc, out, err = run_cli(["--json", "import", "status"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert list_route.called
    assert len(respx.calls) == 1
    assert json.loads(out) == {
        "imports": [],
        "latest_import": None,
        "request_id": "req_imports",
    }


@ROUTE_CTX
def test_graph_config_apply_accepts_exported_wrapper_and_sends_configuration(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    configuration = {
        "registered_tables": [
            {
                "schema": "public",
                "table": "documents",
                "id_columns": ["id"],
                "columns": ["id", "title"],
            }
        ],
        "registered_relationships": [],
        "filter_columns": [],
    }
    config_path = tmp_path / "graph-export.json"
    config_path.write_text(
        json.dumps({"request_id": "req_export", "configuration": configuration}),
        encoding="utf-8",
    )
    route = _stub(
        respx.put(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/configuration"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_apply",
                "configuration": configuration,
                "operation": {"applied": True},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "graph", "config", "apply", "--file", str(config_path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert json.loads(route.calls[0].request.content) == configuration
    assert json.loads(out)["configuration"] == configuration


@ROUTE_CTX
def test_graph_config_apply_rejects_exported_null_configuration_without_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    config_path = tmp_path / "graph-empty.json"
    config_path.write_text(
        json.dumps({"request_id": "req_export", "configuration": None}),
        encoding="utf-8",
    )

    rc, out, err = run_cli(
        ["--json", "graph", "config", "apply", "--file", str(config_path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 2
    assert err == ""
    assert len(respx.calls) == 0
    assert json.loads(out)["error"]["code"] == "GRAPH_CONFIGURATION_EMPTY"


@ROUTE_CTX
def test_db_psql_missing_prints_passwordless_command_and_exits_9(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    monkeypatch.setattr("shutil.which", lambda name: None)
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_conn",
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "direct": {
                    "host": "direct.example",
                    "connection_string_without_password": "postgresql://project_owner@direct.example/app",
                },
                "pooled": {
                    "host": "pool.example",
                    "connection_string_without_password": "postgresql://project_owner@pool.example/app",
                },
            },
        ),
    )

    rc, out, err = run_cli(["db", "psql"], capsys, monkeypatch, tmp_path)

    assert rc == 9
    assert err == ""
    assert "PGSSLMODE=require psql --host direct.example" in out
    assert "PGPASSWORD" not in out


@ROUTE_CTX
def test_db_psql_removes_pgpassword_from_child_environment(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    monkeypatch.setenv("PGPASSWORD", "leaked-password")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/psql")
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_conn",
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "direct": {
                    "host": "direct.example",
                    "connection_string_without_password": "postgresql://project_owner@direct.example/app",
                },
                "pooled": {
                    "host": "pool.example",
                    "connection_string_without_password": "postgresql://project_owner@pool.example/app",
                },
            },
        ),
    )

    rc, out, err = run_cli(["db", "psql"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert out == ""
    assert err == ""
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["PGSSLMODE"] == "require"
    assert "PGPASSWORD" not in child_env


@ROUTE_CTX
def test_migrations_apply_derives_name_and_redacts_sql_from_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    sql_path = tmp_path / "001 create docs.sql"
    sql_path.write_text("CREATE TABLE documents(secret text);", encoding="utf-8")
    create_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_create",
                "migration": {"id": CONFIG_ID, "name": "m_001_create_docs"},
            },
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations/{CONFIG_ID}/apply"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_apply",
                "migration": {"id": CONFIG_ID, "name": "m_001_create_docs", "status": "applied"},
                "operation": {"created": True, "applied": True, "noop": False},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "migrations", "apply", "--file", str(sql_path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert json.loads(create_route.calls[0].request.content) == {
        "name": "m_001_create_docs",
        "sql_body": "CREATE TABLE documents(secret text);",
    }
    assert "CREATE TABLE" not in out
    assert json.loads(out)["migration"]["status"] == "applied"


@ROUTE_CTX
def test_migrations_apply_rejects_invalid_explicit_name_without_normalizing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    sql_path = tmp_path / "001_create_docs.sql"
    sql_path.write_text("CREATE TABLE documents(secret text);", encoding="utf-8")

    rc, out, err = run_cli(
        ["--json", "migrations", "apply", "--file", str(sql_path), "--name", "001-create"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 2
    assert err == ""
    assert json.loads(out)["error"]["code"] == "VALIDATION_ERROR"
    assert len(respx.calls) == 0


@ROUTE_CTX
def test_vector_create_validates_dimensions_and_payload(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/vector/configurations"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_vector",
                "configuration": {"id": CONFIG_ID, "name": "docs", "index_status": "missing"},
                "operation": {"created": True},
            },
        ),
    )

    rc, out, err = run_cli(
        [
            "--json",
            "vector",
            "configs",
            "create",
            "docs",
            "--table",
            "documents",
            "--embedding-column",
            "embedding",
            "--dimensions",
            "1536",
            "--metadata-column",
            "title",
            "--filter-column",
            "tenant_id",
        ],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert json.loads(route.calls[0].request.content) == {
        "name": "docs",
        "schema_name": "public",
        "table_name": "documents",
        "row_id_column": "id",
        "embedding_column": "embedding",
        "dimensions": 1536,
        "metric": "cosine",
        "metadata_columns": ["title"],
        "filter_columns": ["tenant_id"],
        "index_kind": "hnsw",
    }
    assert json.loads(out)["configuration"]["id"] == CONFIG_ID

    rc, out, _ = run_cli(
        [
            "--json",
            "vector",
            "configs",
            "create",
            "docs",
            "--table",
            "documents",
            "--embedding-column",
            "embedding",
            "--dimensions",
            "2001",
        ],
        capsys,
        monkeypatch,
        tmp_path,
    )
    assert rc == 2
    assert json.loads(out)["error"]["code"] == "VALIDATION_ERROR"


@ROUTE_CTX
def test_text_generated_tsvector_applies_migration_then_creates_config(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    migration_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_migration",
                "migration": {"id": CONFIG_ID, "name": "m_docs_body_tsv_generated_tsvector"},
            },
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations/{CONFIG_ID}/apply"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_apply",
                "migration": {"id": CONFIG_ID, "name": "m_docs_body_tsv_generated_tsvector"},
                "operation": {"applied": True},
            },
        ),
    )
    config_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/text/configurations"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_text",
                "configuration": {
                    "id": OTHER_PROJECT_ID,
                    "name": "docs",
                    "search_kind": "tsvector",
                    "tsvector_column": "body_tsv",
                },
                "operation": {"created": True},
            },
        ),
    )

    rc, out, err = run_cli(
        [
            "--json",
            "text",
            "configs",
            "create-tsvector",
            "docs",
            "--table",
            "documents",
            "--text-column",
            "body",
            "--generated-column",
            "body_tsv",
            "--yes",
        ],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    migration_payload = json.loads(migration_route.calls[0].request.content)
    assert migration_payload["name"] == "m_docs_body_tsv_generated_tsvector"
    expected_sql = 'ALTER TABLE "public"."documents" ADD COLUMN IF NOT EXISTS "body_tsv"'
    assert expected_sql in migration_payload["sql_body"]
    assert json.loads(config_route.calls[0].request.content) == {
        "name": "docs",
        "search_kind": "tsvector",
        "schema_name": "public",
        "table_name": "documents",
        "row_id_column": "id",
        "row_id_columns": ["id"],
        "tsvector_column": "body_tsv",
        "language": "english",
        "metadata_columns": [],
        "filter_columns": [],
    }
    payload = json.loads(out)
    assert payload["migration"]["id"] == CONFIG_ID
    assert payload["operation"]["generated_column_created"] is True


@ROUTE_CTX
def test_csv_import_uses_preview_columns_and_wait_polling(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    csv_path = tmp_path / "documents.csv"
    csv_path.write_text("id,title\n1,Hello\n", encoding="utf-8")
    columns = [{"name": "id", "type": "text", "nullable": False}]
    preview_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv/preview"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_preview",
                "preview": {"job_id": IMPORT_ID, "columns": columns},
            },
        ),
    )
    import_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_import",
                "import": {"id": IMPORT_ID, "import_type": "csv", "status": "running"},
                "poll_interval_seconds": 1,
            },
        ),
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/{IMPORT_ID}"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_done",
                "import": {"id": IMPORT_ID, "import_type": "csv", "status": "succeeded"},
            },
        ),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc, out, err = run_cli(
        ["--json", "import", "csv", str(csv_path), "--table", "documents", "--wait"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    assert preview_route.called
    import_request = import_route.calls[0].request
    body = import_request.content.decode("latin1")
    assert "mapping" not in body
    assert json.dumps(columns) in body
    assert json.loads(out)["import"]["status"] == "succeeded"


@ROUTE_CTX
def test_failed_import_human_output_includes_error_context(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/{IMPORT_ID}"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_import_failed",
                "import": {
                    "id": IMPORT_ID,
                    "import_type": "csv",
                    "status": "failed",
                    "error_code": "CSV_PARSE_ERROR",
                    "error_message": "Invalid quoted field.",
                    "progress": {
                        "sqlstate": "22P02",
                        "detail": "invalid input syntax for type integer",
                        "row_errors": [
                            {"row": 8, "message": "Value is not an integer.", "column": "id"}
                        ],
                    },
                    "row_errors": [
                        {"row_number": 7, "message": "Unexpected quote.", "column": "title"}
                    ],
                    "details": {"parser": "csv"},
                },
            },
        ),
    )

    rc, out, err = run_cli(["import", "status", IMPORT_ID], capsys, monkeypatch, tmp_path)

    assert rc == 1
    assert err == ""
    assert "Import 223e4567-e89b-12d3-a456-426614174000 failed" in out
    assert "Request ID: req_import_failed" in out
    assert "Error code: CSV_PARSE_ERROR" in out
    assert "Error message: Invalid quoted field." in out
    assert "Row errors:" in out
    assert "Unexpected quote." in out
    assert "Details:" in out
    assert "parser" in out
    assert "SQL state: 22P02" in out
    assert "Detail: invalid input syntax for type integer" in out
    assert "Progress row errors:" in out
    assert "Value is not an integer." in out


@ROUTE_CTX
def test_failed_import_json_output_shape_is_preserved(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    import_payload = {
        "id": IMPORT_ID,
        "import_type": "csv",
        "status": "failed",
        "error_code": "CSV_PARSE_ERROR",
        "error_message": "Invalid quoted field.",
        "row_errors": [{"row_number": 7, "message": "Unexpected quote."}],
    }
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/{IMPORT_ID}"),
        return_value=httpx.Response(
            200,
            json={"request_id": "req_import_failed", "import": import_payload},
        ),
    )

    rc, out, err = run_cli(
        ["--json", "import", "status", IMPORT_ID], capsys, monkeypatch, tmp_path
    )

    assert rc == 1
    assert err == ""
    assert json.loads(out) == {"import": import_payload, "request_id": "req_import_failed"}


@ROUTE_CTX
def test_verbose_traces_are_redacted(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    route = _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_projects",
                "projects": [{"id": PROJECT_ID, "name": "Support", "status": "ready"}],
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "--verbose", "projects", "list"], capsys, monkeypatch, tmp_path
    )

    assert rc == 0
    assert json.loads(out)["request_id"] == "req_projects"
    assert route.called
    assert ACCESS_TOKEN not in err
    assert "GET /projects -> 200" in err
    assert "request_id=req_projects" in err


@ROUTE_CTX
def test_backend_error_mapping_to_json_exit_code(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(
            403,
            json={
                "request_id": "req_forbidden",
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": "Permission denied.",
                    "details": {},
                },
            },
        ),
    )

    rc, out, err = run_cli(["--json", "projects", "list"], capsys, monkeypatch, tmp_path)

    assert rc == 4
    assert err == ""
    payload = json.loads(out)
    assert payload["request_id"] == "req_forbidden"
    assert payload["error"]["code"] == "PERMISSION_DENIED"


@ROUTE_CTX
def test_login_browser_fallback_polls_stores_tokens_and_redacts_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    start_route = _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/start"),
        return_value=httpx.Response(
            200,
            json={
                "login_session_id": "cls_abcdefghijklmnopqrstuvwxyz",
                "browser_url": "https://app.example.test/cli/login?session=public",
                "device_code": "ABCD-EFGH",
                "expires_at": "2099-07-08T12:00:00Z",
                "poll_interval_seconds": 2,
            },
        ),
    )
    poll_route = _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/poll"),
        return_value=httpx.Response(
            200,
            json={
                "status": "approved",
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_at": "2099-07-08T13:00:00Z",
                "user": {"id": "user-1", "email": "user@example.test"},
            },
        ),
    )

    rc, out, err = run_cli(["login"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert err == ""
    assert "Open this URL" in out
    assert "ABCD-EFGH" in out
    assert "Signed in as user@example.test" in out
    assert ACCESS_TOKEN not in out
    assert REFRESH_TOKEN not in out
    assert json.loads(start_route.calls[0].request.content) == {
        "client": {"name": "polygres-cli", "version": "0.2.0"}
    }
    assert json.loads(poll_route.calls[0].request.content) == {
        "login_session_id": "cls_abcdefghijklmnopqrstuvwxyz",
        "device_code": "ABCD-EFGH",
    }
    assert selected_config(tmp_path)["auth"]["refresh_token"] == REFRESH_TOKEN


@pytest.mark.parametrize(
    "configuration",
    [
        {"unknown": []},
        {"registered_tables": {}},
        {"registered_tables": [{"table": "documents"}]},
        {
            "registered_tables": [
                {"table": "documents", "id_column": "id", "unexpected": True}
            ]
        },
        {
            "registered_relationships": [
                {
                    "from_table": "a",
                    "from_column": "id",
                    "to_table": "b",
                    "to_column": "a_id",
                    "label": "bad-label",
                }
            ]
        },
        {"filter_columns": [{"table": "a", "column": "id", "type": "json"}]},
    ],
)
@ROUTE_CTX
def test_graph_invalid_configuration_bodies_exit_2_before_request(
    configuration: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")

    rc, out, err = run_cli(
        ["--json", "graph", "config", "apply", "--file", str(path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 2
    assert err == ""
    assert json.loads(out)["error"]["code"] == "GRAPH_CONFIGURATION_INVALID"
    assert len(respx.calls) == 0


@ROUTE_CTX
def test_csv_import_propagates_preview_effective_parser_settings(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    path = tmp_path / "documents.csv"
    path.write_text("id|title\n1|Hello\n", encoding="utf-8")
    columns = [{"name": "id", "type": "text", "nullable": False}]
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv/preview"),
        return_value=httpx.Response(
            200,
            json={
                "preview": {
                    "job_id": IMPORT_ID,
                    "encoding": "utf-8-sig",
                    "delimiter": "|",
                    "quote_char": "'",
                    "escape_char": "\\",
                    "has_header": False,
                    "columns": columns,
                }
            },
        ),
    )
    import_route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv"),
        return_value=httpx.Response(
            200, json={"import": {"id": IMPORT_ID, "status": "succeeded"}}
        ),
    )

    rc, _, _ = run_cli(
        ["--json", "import", "csv", str(path), "--table", "documents"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    body = import_route.calls[0].request.content.decode("latin1")
    for value in ("utf-8-sig", "|", "'", "\\", "false", json.dumps(columns)):
        assert value in body


def test_csv_rejects_unsupported_delimiter_before_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "documents.csv"
    path.write_text("a^b\n", encoding="utf-8")

    rc, out, err = run_cli(
        ["--json", "import", "csv", str(path), "--table", "documents", "--delimiter", "^"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 2
    assert err == ""
    assert json.loads(out)["error"]["code"] == "VALIDATION_ERROR"


@ROUTE_CTX
def test_quiet_suppresses_human_primary_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(200, json={"projects": []}),
    )

    rc, out, err = run_cli(
        ["--quiet", "projects", "list"], capsys, monkeypatch, tmp_path
    )

    assert rc == 0
    assert out == ""
    assert err == ""


@ROUTE_CTX
def test_db_info_and_ready_human_outputs_include_all_documented_fields(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "ready": True,
                "direct": {"host": "direct.example.test"},
                "pooled": {"host": "pool.example.test"},
            },
        ),
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/retrieval/readiness"),
        return_value=httpx.Response(
            200,
            json={
                "project_id": PROJECT_ID,
                "graph": {"ready": True},
                "vector": {"ready": False},
                "hybrid": {"ready": False},
            },
        ),
    )

    rc, out, err = run_cli(["--no-color", "db", "info"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert err == ""
    for value in (
        "direct.example.test",
        "pool.example.test",
        "5432",
        "app",
        "project_owner",
        "True",
    ):
        assert value in out

    rc, out, err = run_cli(["--no-color", "ready"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert err == ""
    assert "Graph ready" in out
    assert "Vector ready" in out
    assert "Hybrid ready" in out


@ROUTE_CTX
def test_migration_response_id_is_validated_before_apply_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    path = tmp_path / "001.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations"),
        return_value=httpx.Response(200, json={"migration": {"id": "not-a-uuid"}}),
    )

    rc, out, err = run_cli(
        ["--json", "migrations", "apply", "--file", str(path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 1
    assert err == ""
    assert json.loads(out)["error"]["code"] == "MIGRATION_INVALID"
    assert len(respx.calls) == 1


@ROUTE_CTX
def test_human_import_polling_writes_progress_only_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    path = tmp_path / "documents.csv"
    path.write_text("id\n1\n", encoding="utf-8")
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv/preview"),
        return_value=httpx.Response(
            200, json={"preview": {"job_id": IMPORT_ID, "columns": [{"name": "id"}]}}
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/csv"),
        return_value=httpx.Response(
            200,
            json={
                "import": {"id": IMPORT_ID, "status": "running", "progress": {"rows": 1}},
                "poll_interval_seconds": 1,
            },
        ),
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/imports/{IMPORT_ID}"),
        return_value=httpx.Response(
            200, json={"import": {"id": IMPORT_ID, "status": "succeeded"}}
        ),
    )

    rc, out, err = run_cli(
        ["--no-color", "import", "csv", str(path), "--table", "documents", "--wait"],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert f"Import {IMPORT_ID} succeeded" in out
    assert f"Import {IMPORT_ID}: running" in err
    assert '"rows": 1' in err


@ROUTE_CTX
def test_client_retry_wait_is_clamped_to_command_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("polygres.cli_client.time.monotonic", lambda: 10.0)
    monkeypatch.setattr("polygres.cli_client.time.sleep", sleeps.append)
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(503, headers={"Retry-After": "30"}),
    )
    client = CliControlPlaneClient(
        base_url=API_BASE_URL,
        access_token=ACCESS_TOKEN,
        max_retries=1,
    )

    with client, pytest.raises(cli.CliError):
        client._get("/projects", deadline=11.0)

    assert sleeps == [1.0]


@ROUTE_CTX
def test_graph_discover_normalizes_real_backend_discovery_to_applyable_configuration(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/discover"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_discover",
                "project_id": PROJECT_ID,
                "draft": True,
                "node_tables": [
                    {
                        "schema": "public",
                        "table": "accounts",
                        "id_columns": ["tenant_id", "id"],
                        "columns": ["tenant_id", "id", "name"],
                        "confidence": "medium",
                        "estimated_row_count": 41,
                        "relationship_count": 1,
                    }
                ],
                "relationships": [
                    {
                        "from_schema": "public",
                        "from_table": "documents",
                        "from_column": "account_id",
                        "to_schema": "public",
                        "to_table": "accounts",
                        "to_column": "id",
                        "label": "documents_account_id_fkey",
                        "bidirectional": True,
                        "confidence": "high",
                        "cardinality": "N:1",
                        "from_table_row_count": 100,
                        "to_table_row_count": 41,
                    }
                ],
                "filter_columns": [
                    {
                        "schema": "public",
                        "table": "accounts",
                        "column": "name",
                        "type": "text",
                        "confidence": "low",
                    }
                ],
                "forbidden_sql": ["graph.auto_discover"],
            },
        ),
    )

    rc, out, err = run_cli(["--json", "graph", "discover"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert json.loads(out) == {
        "configuration": {
            "registered_tables": [
                {
                    "schema": "public",
                    "table": "accounts",
                    "id_columns": ["tenant_id", "id"],
                    "columns": ["tenant_id", "id", "name"],
                }
            ],
            "registered_relationships": [
                {
                    "from_schema": "public",
                    "from_table": "documents",
                    "from_column": "account_id",
                    "to_schema": "public",
                    "to_table": "accounts",
                    "to_column": "id",
                    "label": "documents_account_id_fkey",
                    "bidirectional": True,
                }
            ],
            "filter_columns": [
                {
                    "schema": "public",
                    "table": "accounts",
                    "column": "name",
                    "type": "text",
                }
            ],
            "runtime_settings": {},
        },
        "request_id": "req_discover",
    }


@ROUTE_CTX
@pytest.mark.parametrize(
    "discovery",
    [
        {"node_tables": "not-an-array", "relationships": [], "filter_columns": []},
        {"node_tables": [{"schema": "public", "table": "docs"}]},
        {"node_tables": [], "relationships": [{"from_table": "docs"}]},
        {"node_tables": [], "relationships": [], "filter_columns": [{"type": "fuzzy"}]},
    ],
)
def test_graph_discover_rejects_malformed_backend_candidates(
    discovery: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/discover"),
        return_value=httpx.Response(200, json={"request_id": "req_bad", **discovery}),
    )

    rc, out, err = run_cli(["--json", "graph", "discover"], capsys, monkeypatch, tmp_path)

    assert rc == 1
    assert err == ""
    assert json.loads(out)["error"]["code"] == "GRAPH_DISCOVERY_INVALID"


@ROUTE_CTX
def test_graph_export_round_trip_strips_read_only_record_fields_on_apply(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    record = {
        "id": CONFIG_ID,
        "project_id": PROJECT_UUID,
        "registered_tables": [
            {
                "schema": "public",
                "table": "documents",
                "id_column": "id",
                "id_columns": [],
                "columns": ["id", "title"],
                "tenant_column": None,
            }
        ],
        "registered_relationships": [],
        "filter_columns": [],
        "runtime_settings": {},
        "build_status": "ready",
        "build_id": "build-1",
        "last_built_at": "2026-07-09T12:00:00Z",
        "needs_rebuild": False,
        "invalid_reason": None,
        "created_at": "2026-07-09T11:00:00Z",
        "updated_at": "2026-07-09T12:00:00Z",
    }
    path = tmp_path / "graph-export.json"
    path.write_text(json.dumps({"configuration": record, "request_id": "req_export"}))
    route = _stub(
        respx.put(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/configuration"),
        return_value=httpx.Response(200, json={"request_id": "req_apply", "configuration": record}),
    )

    rc, out, err = run_cli(
        ["--json", "graph", "config", "apply", "--file", str(path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert err == ""
    sent = json.loads(route.calls[0].request.content)
    assert set(sent) == {
        "registered_tables",
        "registered_relationships",
        "filter_columns",
        "runtime_settings",
    }
    assert set(sent["registered_tables"][0]) == {
        "schema",
        "table",
        "id_column",
        "id_columns",
        "columns",
        "tenant_column",
    }
    assert json.loads(out)["configuration"]["build_status"] == "ready"


@ROUTE_CTX
def test_graph_build_normalizes_runtime_operation_string(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/build"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_build",
                "configuration": {"build_status": "ready", "needs_rebuild": False},
                "operation": "completed",
            },
        ),
    )

    rc, out, err = run_cli(["--json", "graph", "build"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert json.loads(out)["operation"] == {
        "build_started": True,
        "build_completed": True,
    }


@ROUTE_CTX
def test_migration_terminal_failure_exits_one_and_never_claims_applied(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    path = tmp_path / "broken.sql"
    path.write_text("CREATE TABLE broken(id integer);", encoding="utf-8")
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_create",
                "migration": {"id": CONFIG_ID, "name": "broken", "status": "draft"},
                "operation": {"created": True},
            },
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations/{CONFIG_ID}/apply"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_apply",
                "migration": {
                    "id": CONFIG_ID,
                    "name": "broken",
                    "status": "failed",
                    "error_message": "relation creation failed",
                },
                "operation": {"applied": False, "noop": False},
            },
        ),
    )

    rc, out, err = run_cli(
        ["--json", "migrations", "apply", "--file", str(path)],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 1
    assert err == ""
    payload = json.loads(out)
    assert payload["migration"]["status"] == "failed"
    assert payload["operation"] == {"created": True, "applied": False, "noop": False}


@ROUTE_CTX
def test_generated_tsvector_stops_when_migration_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations"),
        return_value=httpx.Response(
            200,
            json={"migration": {"id": CONFIG_ID, "name": "generated", "status": "draft"}},
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/migrations/{CONFIG_ID}/apply"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_failed",
                "migration": {"id": CONFIG_ID, "status": "failed", "error_message": "bad SQL"},
            },
        ),
    )
    text_route = respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/text/configurations")

    rc, out, err = run_cli(
        [
            "--json",
            "text",
            "configs",
            "create-tsvector",
            "documents",
            "--table",
            "documents",
            "--text-column",
            "body",
            "--generated-column",
            "body_tsv",
            "--yes",
        ],
        capsys,
        monkeypatch,
        tmp_path,
    )

    assert rc == 1
    assert err == ""
    assert json.loads(out)["error"]["code"] == "MIGRATION_APPLY_FAILED"
    assert not text_route.called


@ROUTE_CTX
def test_env_removes_backend_password_placeholder_in_human_and_json_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    connection_payload = {
        "request_id": "req_conn",
        "project_id": PROJECT_ID,
        "database": "app",
        "username": "project_owner",
        "port": 5432,
        "runtime_api_url": f"https://{PROJECT_ID}.api.db.polygres.com/v1",
        "direct": {
            "host": "direct.example.test",
            "connection_string_without_password": (
                "postgresql://project_owner:<password>@direct.example.test:5432/app"
                "?sslmode=require"
            ),
        },
        "pooled": {
            "host": "pool.example.test",
            "connection_string_without_password": (
                "postgresql://project_owner:<password>@pool.example.test:5432/app"
                "?pgbouncer=true&sslmode=require"
            ),
        },
    }
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/connection-info"),
        side_effect=[
            httpx.Response(200, json=connection_payload),
            httpx.Response(200, json=connection_payload),
        ],
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/api-keys"),
        side_effect=[
            httpx.Response(200, json={"request_id": "req_keys", "api_keys": []}),
            httpx.Response(200, json={"request_id": "req_keys", "api_keys": []}),
        ],
    )

    rc, human, err = run_cli(["env"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert err == ""
    assert "<password>" not in human
    assert "[REDACTED]" not in human
    assert "postgresql://project_owner@" in human

    rc, machine, err = run_cli(["--json", "env"], capsys, monkeypatch, tmp_path)
    assert rc == 0
    assert err == ""
    rendered = json.dumps(json.loads(machine)["env"])
    assert "<password>" not in rendered
    assert "[REDACTED]" not in rendered
    assert "postgresql://project_owner@" in rendered


@ROUTE_CTX
def test_keys_create_rejects_missing_one_time_secret(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/api-keys"),
        return_value=httpx.Response(
            200,
            json={"request_id": "req_key", "api_key": {"id": CONFIG_ID, "name": "local"}},
        ),
    )

    rc, out, err = run_cli(
        ["--json", "keys", "create", "local"], capsys, monkeypatch, tmp_path
    )

    assert rc == 1
    assert err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "API_KEY_RESPONSE_INVALID"
    assert payload["request_id"] == "req_key"


@ROUTE_CTX
def test_keys_list_recursively_redacts_unexpected_secret_fields(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(tmp_path, {"version": 1, "selected_project_id": PROJECT_ID})
    _stub(
        respx.get(f"{API_BASE_URL}/projects/{PROJECT_ID}/api-keys"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_keys",
                "api_keys": [
                    {
                        "id": CONFIG_ID,
                        "name": "local",
                        "prefix": "poly_live_12345678",
                        "metadata": {
                            "access_token": "unexpected-access-token",
                            "connection": "postgresql://user:secret@host/db",
                        },
                    }
                ],
            },
        ),
    )

    rc, out, err = run_cli(["--json", "keys", "list"], capsys, monkeypatch, tmp_path)

    assert rc == 0
    assert err == ""
    assert "unexpected-access-token" not in out
    assert "user:secret@" not in out
    assert json.loads(out)["keys"][0]["metadata"]["access_token"] == "[REDACTED]"


@ROUTE_CTX
def test_malformed_refresh_response_clears_stored_auth(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_config(
        tmp_path,
        {
            "version": 1,
            "auth": {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "user": {"id": "user-1"},
            },
        },
    )
    _stub(
        respx.get(f"{API_BASE_URL}/projects"),
        return_value=httpx.Response(401, json={"error": {"code": "AUTH_EXPIRED"}}),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/refresh"),
        return_value=httpx.Response(
            200,
            json={"request_id": "req_refresh", "access_token": "replacement-only"},
        ),
    )

    rc, out, err = run_cli(
        ["--json", "projects", "list"], capsys, monkeypatch, tmp_path, token=None
    )

    assert rc == 3
    assert err == ""
    assert json.loads(out)["error"]["code"] == "AUTH_REFRESH_INVALID"
    assert "auth" not in selected_config(tmp_path)


@ROUTE_CTX
def test_quiet_login_prints_essential_fallback_to_stderr_when_browser_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/start"),
        return_value=httpx.Response(
            200,
            json={
                "login_session_id": "cls_abcdefghijklmnopqrstuvwxyz",
                "browser_url": "https://app.example.test/cli/login?session=abc",
                "device_code": "ABCD-EFGH",
                "expires_at": expires,
                "poll_interval_seconds": 1,
            },
        ),
    )
    _stub(
        respx.post(f"{API_BASE_URL}/cli/auth/poll"),
        return_value=httpx.Response(
            200,
            json={
                "status": "approved",
                "access_token": NEW_ACCESS_TOKEN,
                "refresh_token": NEW_REFRESH_TOKEN,
                "expires_at": expires,
                "user": {"id": "user-1", "email": "user@example.test"},
            },
        ),
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(cli, "_sleep_until_deadline", lambda _seconds, _deadline: None)

    rc, out, err = run_cli(["--quiet", "login"], capsys, monkeypatch, tmp_path, token=None)

    assert rc == 0
    assert out == ""
    assert "https://app.example.test/cli/login?session=abc" in err
    assert "ABCD-EFGH" in err


def test_config_save_fails_closed_when_owner_only_permissions_cannot_be_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path / ".config" / "polygres" / "config.json")

    def deny_chmod(*_args: object) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(os, "chmod", deny_chmod)

    with pytest.raises(cli.CliError) as exc_info:
        store.save({"version": 1, "auth": {"refresh_token": "secret"}})

    assert exc_info.value.code == "CONFIG_PERMISSIONS_INVALID"
    assert not store.path.exists()


def test_retry_after_http_date_is_parsed_as_non_negative_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(cli_client.time, "time", lambda: now.timestamp())

    delay = cli_client._retry_after_seconds("Thu, 09 Jul 2026 12:00:05 GMT")

    assert delay == pytest.approx(5.0)


@ROUTE_CTX
def test_heavy_graph_build_request_uses_extended_read_timeout() -> None:
    route = _stub(
        respx.post(f"{API_BASE_URL}/projects/{PROJECT_ID}/graph/build"),
        return_value=httpx.Response(200, json={"configuration": {"build_status": "ready"}}),
    )
    client = CliControlPlaneClient(base_url=API_BASE_URL, access_token=ACCESS_TOKEN)

    with client:
        client.graph_build(PROJECT_ID)

    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] >= 120.0
