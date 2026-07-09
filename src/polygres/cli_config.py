from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from polygres.cli_errors import GENERAL_FAILURE, CliError

DEFAULT_API_BASE_URL = "https://api.polygres.com/v1"
CONFIG_VERSION = 1


def default_config_path() -> Path:
    return Path.home() / ".config" / "polygres" / "config.json"


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CONFIG_VERSION, "api_base_url": DEFAULT_API_BASE_URL}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CliError(
                code="CONFIG_INVALID",
                message=f"Invalid Polygres config JSON at {self.path}.",
                exit_code=GENERAL_FAILURE,
            ) from exc
        if not isinstance(payload, dict):
            raise CliError(
                code="CONFIG_INVALID",
                message=f"Invalid Polygres config JSON at {self.path}.",
                exit_code=GENERAL_FAILURE,
            )
        payload.setdefault("version", CONFIG_VERSION)
        payload.setdefault("api_base_url", DEFAULT_API_BASE_URL)
        return payload

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_owner_only(self.path.parent, 0o700)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _chmod_owner_only(temp_path, 0o600)
        os.replace(temp_path, self.path)
        _chmod_owner_only(self.path, 0o600)


def resolve_api_base_url(config: dict[str, Any]) -> str:
    return os.environ.get("POLYGRES_API_BASE_URL") or str(
        config.get("api_base_url") or DEFAULT_API_BASE_URL
    )


def access_token(config: dict[str, Any]) -> str | None:
    env_token = os.environ.get("POLYGRES_ACCESS_TOKEN")
    if env_token:
        return env_token
    auth = config.get("auth")
    if isinstance(auth, dict) and isinstance(auth.get("access_token"), str):
        return auth["access_token"]
    return None


def env_access_token_set() -> bool:
    return bool(os.environ.get("POLYGRES_ACCESS_TOKEN"))


def refresh_token(config: dict[str, Any]) -> str | None:
    auth = config.get("auth")
    if isinstance(auth, dict) and isinstance(auth.get("refresh_token"), str):
        return auth["refresh_token"]
    return None


def _chmod_owner_only(path: Path, mode: int) -> None:
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise CliError(
            code="CONFIG_PERMISSIONS_INVALID",
            message=f"Unable to set owner-only permissions on {path}.",
            exit_code=GENERAL_FAILURE,
        ) from exc
