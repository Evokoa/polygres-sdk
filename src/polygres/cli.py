from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from polygres.cli_auth import clear_auth, validate_start_response, validated_approved_auth
from polygres.cli_client import VERSION, CliControlPlaneClient
from polygres.cli_config import (
    ConfigStore,
    access_token,
    env_access_token_set,
    refresh_token,
    resolve_api_base_url,
)
from polygres.cli_errors import (
    AUTH,
    CONFLICT,
    GENERAL_FAILURE,
    LOCAL_DEPENDENCY,
    NOT_FOUND,
    SUCCESS,
    UNAVAILABLE,
    USAGE,
    CliError,
    UsageError,
)
from polygres.cli_output import print_kv, print_table, write_error, write_json
from polygres.cli_secrets import redact

PROJECT_ID_RE = re.compile(r"^p[a-z0-9]{23}$")
UUID_LIKE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MIGRATION_NAME_RE = SQL_IDENTIFIER_RE
MAX_CSV_UPLOAD_BYTES = 1024**3
GRAPH_CONFIGURATION_KEYS = {
    "registered_tables",
    "registered_relationships",
    "filter_columns",
    "runtime_settings",
}
GRAPH_CONFIGURATION_READ_ONLY_KEYS = {
    "id",
    "project_id",
    "build_status",
    "build_id",
    "last_built_at",
    "needs_rebuild",
    "invalid_reason",
    "created_at",
    "updated_at",
}


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        code = "INVALID_USAGE" if "invalid choice" in message else "VALIDATION_ERROR"
        raise UsageError(message, code=code)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    json_output = "--json" in argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "version", False):
            sys.stdout.write(f"polygres {VERSION}\n")
            return SUCCESS
        if not hasattr(args, "func"):
            parser.print_help()
            return SUCCESS
        ctx = _context(args)
        with ctx.client:
            return int(args.func(ctx, args))
    except SystemExit as exc:
        return int(exc.code or SUCCESS)
    except CliError as exc:
        write_error(exc, json_output=json_output)
        return exc.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="polygres", description="Polygres command line tool")
    parser.add_argument("--version", action="store_true", help="print the installed CLI version")
    parser.add_argument("--json", action="store_true", help="write machine-readable JSON")
    parser.add_argument("--project", help="project ID or exact project name")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color output")
    parser.add_argument("--quiet", action="store_true", help="suppress non-essential output")
    parser.add_argument("--verbose", action="store_true", help="print redacted request traces")
    subparsers = parser.add_subparsers(dest="resource", metavar="<resource>")

    _add_auth_parsers(subparsers)
    _add_projects_parsers(subparsers)
    _add_env_parser(subparsers)
    _add_db_parsers(subparsers)
    _add_keys_parsers(subparsers)
    _add_import_parsers(subparsers)
    _add_migration_parsers(subparsers)
    _add_graph_parsers(subparsers)
    _add_vector_parsers(subparsers)
    _add_text_parsers(subparsers)
    _add_ready_parser(subparsers)
    _add_config_parsers(subparsers)
    return parser


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.store = ConfigStore()
        self.config = self.store.load()
        stored_refresh_token = None if env_access_token_set() else refresh_token(self.config)
        self.client = CliControlPlaneClient(
            base_url=resolve_api_base_url(self.config),
            access_token=access_token(self.config),
            refresh_token=stored_refresh_token,
            on_token_refresh=self.store_refreshed_auth,
            on_refresh_auth_failure=self.clear_stored_auth,
            verbose=bool(args.verbose),
            trace=lambda line: sys.stderr.write(line + "\n"),
        )

    @property
    def json(self) -> bool:
        return bool(self.args.json)

    @property
    def quiet(self) -> bool:
        return bool(self.args.quiet)

    @property
    def selected_project_id(self) -> str | None:
        value = self.config.get("selected_project_id")
        return value if isinstance(value, str) else None

    def save(self) -> None:
        self.store.save(self.config)

    def store_refreshed_auth(self, payload: dict[str, Any]) -> None:
        user = payload.get("user")
        self.config["auth"] = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": payload.get("expires_at"),
            "user": user if isinstance(user, dict) else {},
        }
        self.save()

    def clear_stored_auth(self) -> None:
        clear_auth(self.config)
        self.save()


def _context(args: argparse.Namespace) -> Context:
    return Context(args)


def _add_auth_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    login = subparsers.add_parser("login", help="sign in through the browser")
    login.add_argument("--timeout", type=_timeout_seconds, default=600)
    login.set_defaults(func=handle_login)
    logout = subparsers.add_parser("logout", help="sign out and remove local credentials")
    logout.set_defaults(func=handle_logout)
    whoami = subparsers.add_parser("whoami", help="show authenticated user")
    whoami.set_defaults(func=handle_whoami)


def _add_projects_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("projects", help="manage projects")
    sub = parser.add_subparsers(dest="action", required=True)
    list_parser = sub.add_parser("list", help="list projects")
    list_parser.set_defaults(func=handle_projects_list)
    use_parser = sub.add_parser("use", help="select a project")
    use_parser.add_argument("project")
    use_parser.set_defaults(func=handle_projects_use)
    create_parser = sub.add_parser("create", help="create a project")
    create_parser.add_argument("name")
    create_parser.add_argument("--no-wait", action="store_true")
    create_parser.add_argument("--timeout", type=_timeout_seconds, default=600)
    create_parser.set_defaults(func=handle_projects_create)
    status_parser = sub.add_parser("status", help="show project status")
    status_parser.add_argument("project", nargs="?")
    status_parser.set_defaults(func=handle_projects_status)


def _add_env_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("env", help="print project environment variables")
    parser.set_defaults(func=handle_env)


def _add_db_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("db", help="database commands")
    sub = parser.add_subparsers(dest="action", required=True)
    info = sub.add_parser("info", help="show database connection metadata")
    info.set_defaults(func=handle_db_info)
    psql = sub.add_parser("psql", help="open psql")
    psql.set_defaults(func=handle_db_psql)


def _add_keys_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("keys", help="manage runtime API keys")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="create an API key")
    create.add_argument("name")
    create.set_defaults(func=handle_keys_create)
    list_parser = sub.add_parser("list", help="list API keys")
    list_parser.set_defaults(func=handle_keys_list)
    revoke = sub.add_parser("revoke", help="revoke an API key")
    revoke.add_argument("key_id")
    revoke.add_argument("--yes", action="store_true")
    revoke.set_defaults(func=handle_keys_revoke)


def _add_import_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("import", help="import data")
    sub = parser.add_subparsers(dest="kind", required=True)
    csv_parser = sub.add_parser("csv", help="import CSV data")
    csv_parser.add_argument("file")
    csv_parser.add_argument("--table", required=True)
    csv_parser.add_argument("--schema", default="public")
    csv_parser.add_argument(
        "--mode",
        choices=["create_table", "append_existing", "replace_existing"],
        default="create_table",
    )
    csv_parser.add_argument("--encoding", choices=["utf-8", "utf-8-sig"], default="utf-8")
    csv_parser.add_argument("--delimiter", type=_delimiter)
    csv_parser.add_argument("--quote-char", type=_one_char)
    csv_parser.add_argument("--escape-char", type=_one_char)
    csv_parser.add_argument("--no-header", action="store_true")
    csv_parser.add_argument("--wait", action="store_true")
    csv_parser.add_argument("--timeout", type=_timeout_seconds, default=1800)
    csv_parser.set_defaults(func=handle_import_csv)
    status = sub.add_parser("status", help="show import status")
    status.add_argument("job_id", nargs="?")
    status.set_defaults(func=handle_import_status)


def _add_migration_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("migrations", help="manage migrations")
    sub = parser.add_subparsers(dest="action", required=True)
    list_parser = sub.add_parser("list", help="list migrations")
    list_parser.set_defaults(func=handle_migrations_list)
    apply = sub.add_parser("apply", help="apply a SQL migration")
    apply.add_argument("--file", required=True)
    apply.add_argument("--name")
    apply.set_defaults(func=handle_migrations_apply)


def _add_graph_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("graph", help="manage graph retrieval")
    sub = parser.add_subparsers(dest="action", required=True)
    discover = sub.add_parser("discover", help="discover graph configuration")
    discover.set_defaults(func=handle_graph_discover)
    config = sub.add_parser("config", help="graph configuration")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    export = config_sub.add_parser("export", help="export graph configuration")
    export.set_defaults(func=handle_graph_config_export)
    apply = config_sub.add_parser("apply", help="apply graph configuration")
    apply.add_argument("--file", required=True)
    apply.set_defaults(func=handle_graph_config_apply)
    build = sub.add_parser("build", help="build graph index")
    build.set_defaults(func=handle_graph_build)
    status = sub.add_parser("status", help="show graph status")
    status.set_defaults(func=handle_graph_status)


def _add_vector_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("vector", help="manage vector retrieval")
    sub = parser.add_subparsers(dest="action", required=True)
    configs = sub.add_parser("configs", help="vector configurations")
    configs_sub = configs.add_subparsers(dest="configs_action", required=True)
    list_parser = configs_sub.add_parser("list", help="list vector configurations")
    list_parser.set_defaults(func=handle_vector_configs_list)
    create = configs_sub.add_parser("create", help="create vector configuration")
    create.add_argument("name")
    create.add_argument("--table", required=True)
    create.add_argument("--embedding-column", required=True)
    create.add_argument("--dimensions", type=_dimensions, required=True)
    create.add_argument("--schema", default="public")
    create.add_argument("--row-id-column", default="id")
    create.add_argument("--metric", choices=["cosine", "inner_product", "l2"], default="cosine")
    create.add_argument("--index-kind", choices=["hnsw", "none"], default="hnsw")
    create.add_argument("--metadata-column", action="append", default=[])
    create.add_argument("--filter-column", action="append", default=[])
    create.set_defaults(func=handle_vector_configs_create)
    delete = configs_sub.add_parser("delete", help="delete vector configuration")
    delete.add_argument("config_id")
    delete.add_argument("--yes", action="store_true")
    delete.set_defaults(func=handle_vector_configs_delete)
    reindex = sub.add_parser("reindex", help="reindex vector configuration")
    reindex.add_argument("config_id")
    reindex.set_defaults(func=handle_vector_reindex)


def _add_text_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("text", help="manage text retrieval")
    sub = parser.add_subparsers(dest="action", required=True)
    configs = sub.add_parser("configs", help="text configurations")
    configs_sub = configs.add_subparsers(dest="configs_action", required=True)
    list_parser = configs_sub.add_parser("list", help="list text configurations")
    list_parser.set_defaults(func=handle_text_configs_list)
    tsv = configs_sub.add_parser("create-tsvector", help="create TSVector configuration")
    tsv.add_argument("name")
    tsv.add_argument("--table", required=True)
    tsv.add_argument("--tsvector-column")
    tsv.add_argument("--text-column")
    tsv.add_argument("--generated-column")
    tsv.add_argument("--schema", default="public")
    tsv.add_argument("--row-id-column", default="id")
    tsv.add_argument("--language", default="english")
    tsv.add_argument("--metadata-column", action="append", default=[])
    tsv.add_argument("--filter-column", action="append", default=[])
    tsv.add_argument("--yes", action="store_true")
    tsv.set_defaults(func=handle_text_create_tsvector)
    fuzzy = configs_sub.add_parser("create-fuzzy", help="create fuzzy text configuration")
    fuzzy.add_argument("name")
    fuzzy.add_argument("--table", required=True)
    fuzzy.add_argument("--text-column", required=True)
    fuzzy.add_argument("--schema", default="public")
    fuzzy.add_argument("--row-id-column", default="id")
    fuzzy.add_argument("--language", default="english")
    fuzzy.add_argument("--similarity-threshold", type=_similarity_threshold, default=0.3)
    fuzzy.add_argument("--metadata-column", action="append", default=[])
    fuzzy.add_argument("--filter-column", action="append", default=[])
    fuzzy.set_defaults(func=handle_text_create_fuzzy)
    delete = configs_sub.add_parser("delete", help="delete text configuration")
    delete.add_argument("config_id")
    delete.add_argument("--yes", action="store_true")
    delete.set_defaults(func=handle_text_configs_delete)


def _add_ready_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("ready", help="show retrieval readiness")
    parser.set_defaults(func=handle_ready)


def _add_config_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("config", help="local CLI configuration")
    sub = parser.add_subparsers(dest="action", required=True)
    path = sub.add_parser("path", help="print config path")
    path.set_defaults(func=handle_config_path)


def handle_login(ctx: Context, args: argparse.Namespace) -> int:
    started = ctx.client.start_login({"name": "polygres-cli", "version": VERSION})
    session_id, browser_url, device_code, expires_at, interval = validate_start_response(started)
    opened = False
    try:
        opened = bool(webbrowser.open(browser_url))
    except Exception:  # Browser integrations are platform-specific and fallback is required.
        opened = False
    if not ctx.json and not args.quiet:
        if opened:
            sys.stdout.write("Opened a browser to complete sign-in.\n")
        else:
            sys.stdout.write("Open this URL to complete sign-in:\n")
        sys.stdout.write(f"{browser_url}\nDevice code: {device_code}\n")
        sys.stdout.write(f"Expires: {started['expires_at']}\n")
    elif not opened:
        sys.stderr.write("Open this URL to complete sign-in:\n")
        sys.stderr.write(f"{browser_url}\nDevice code: {device_code}\n")
        sys.stderr.write(f"Expires: {started['expires_at']}\n")

    now = datetime.now(timezone.utc)
    session_seconds = max((expires_at - now).total_seconds(), 0.0)
    deadline = time.monotonic() + min(float(args.timeout), session_seconds)
    status = "pending"
    last_payload: dict[str, Any] = {"status": status}
    while time.monotonic() < deadline:
        _sleep_until_deadline(interval, deadline)
        if time.monotonic() >= deadline:
            break
        last_payload = ctx.client.poll_login(session_id, device_code, deadline=deadline)
        status = last_payload.get("status")
        if status == "pending":
            interval = _poll_interval(last_payload)
            continue
        if status == "approved":
            auth = validated_approved_auth(last_payload)
            ctx.config["auth"] = auth
            ctx.save()
            output = {"authenticated": True, "user": auth["user"]}
            if ctx.json:
                write_json(output)
            elif not args.quiet:
                user = auth["user"]
                sys.stdout.write(f"Signed in as {user.get('email') or user.get('id') or 'user'}.\n")
            return SUCCESS
        if status == "denied":
            raise CliError("AUTH_DENIED", "Sign-in was denied.", exit_code=AUTH)
        if status == "expired":
            raise CliError("AUTH_EXPIRED", "Sign-in session expired.", exit_code=AUTH)
        raise CliError(
            "AUTH_RESPONSE_INVALID",
            "Authentication poll returned an unknown status.",
            exit_code=AUTH,
            details={"status": status},
        )
    if expires_at <= datetime.now(timezone.utc):
        raise CliError("AUTH_EXPIRED", "Sign-in session expired.", exit_code=AUTH)
    raise CliError(
        "AUTH_TIMEOUT",
        "Timed out waiting for sign-in approval.",
        exit_code=UNAVAILABLE,
        details={"status": status, "expires_at": started.get("expires_at")},
    )


def handle_logout(ctx: Context, args: argparse.Namespace) -> int:
    token = refresh_token(ctx.config)
    if token:
        try:
            ctx.client.revoke_login(token)
        except CliError:
            pass
    clear_auth(ctx.config)
    ctx.save()
    if ctx.json:
        write_json({"logged_out": True})
    elif not args.quiet:
        sys.stdout.write("Logged out.\n")
    return SUCCESS


def handle_whoami(ctx: Context, args: argparse.Namespace) -> int:
    payload = ctx.client.me()
    output = {
        "profile": payload.get("profile") or payload.get("user") or {},
        "organization": payload.get("organization") or {},
        "membership": payload.get("membership") or {},
        "project_count": payload.get("project_count", 0),
        "gate_destination": payload.get("gate_destination"),
        "request_id": payload.get("request_id"),
    }
    organization = output["organization"]
    org_label = ""
    if isinstance(organization, dict):
        org_label = str(organization.get("name") or organization.get("id") or "")
        role = organization.get("role")
        if role:
            org_label = f"{org_label} ({role})" if org_label else str(role)
    return _emit(
        ctx,
        output,
        [("User", output["profile"].get("email", "")), ("Organization", org_label)],
    )


def handle_projects_list(ctx: Context, args: argparse.Namespace) -> int:
    payload = ctx.client.list_projects()
    projects = _items(payload, "projects")
    output = {
        "projects": projects,
        "selected_project_id": ctx.selected_project_id,
        "request_id": payload.get("request_id"),
    }
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        columns = (
            ["external_id", "name", "status"]
            if _has_external_ids(projects)
            else ["id", "name", "status"]
        )
        print_table(output["projects"], columns)
    return SUCCESS


def handle_projects_use(ctx: Context, args: argparse.Namespace) -> int:
    project = _resolve_project(ctx, args.project)
    project_id = _project_api_id(project)
    ctx.config["selected_project_id"] = project_id
    ctx.save()
    output = {
        "project": project,
        "selected_project_id": project_id,
        "request_id": project.get("request_id"),
    }
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        sys.stdout.write(
            f"Selected project: {project.get('name', project_id)} ({project_id})\n"
        )
    return SUCCESS


def handle_projects_create(ctx: Context, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    payload = ctx.client.create_project(
        args.name, request_timeout=float(args.timeout), deadline=deadline
    )
    project = _object(payload, "project")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else None
    project_id = _project_api_id(project)
    if not args.no_wait:
        try:
            status = _poll_project_status(ctx, project_id, deadline=deadline)
        except CliError as exc:
            raise _project_create_wait_error(
                project=project,
                project_id=project_id,
                create_request_id=payload.get("request_id"),
                cause=exc,
            ) from exc
    output = {"project": project, "request_id": payload.get("request_id")}
    if status is not None:
        output["status"] = status
    return _emit(
        ctx,
        output,
        [
            ("Project", project.get("external_id") or project.get("id", "")),
            (
                "Status",
                (status.get("project") or status.get("status"))
                if isinstance(status, dict)
                else project.get("status", ""),
            ),
        ],
    )


def handle_projects_status(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, args.project)
    payload = ctx.client.get_project_status(project_id)
    output = _project_status_output(project_id, payload)
    return _emit(
        ctx,
        output,
        [
            ("Project", project_id),
            ("Project status", output["project"].get("status", "")),
            ("Runtime status", _summary_value(output["runtime"])),
            ("Resource pressure", _resource_pressure(output["resources"])),
            ("Readiness", _summary_value(output["readiness"])),
        ],
    )


def handle_env(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    conn = ctx.client.connection_info(project_id)
    keys = ctx.client.list_api_keys(project_id)
    output = {
        "env": {
            "DATABASE_URL": _remove_pgbouncer_query(
                conn.get("pooled", {}).get("connection_string_without_password")
            ),
            "DIRECT_URL": _passwordless_url(
                conn.get("direct", {}).get("connection_string_without_password")
            ),
            "POLYGRES_RUNTIME_URL": conn.get("runtime_api_url")
            or conn.get("runtime", {}).get("url"),
        },
        "api_keys": [_sanitize_key(key) for key in _items(keys, "api_keys", "keys")],
        "request_id": conn.get("request_id") or keys.get("request_id"),
    }
    if ctx.json:
        write_json(redact(output))
    elif not ctx.quiet:
        for key, value in output["env"].items():
            if value:
                sys.stdout.write(f"export {key}={shlex.quote(str(value))}\n")
        sys.stdout.write("# POLYGRES_API_KEY is not shown by default. Create one with:\n")
        sys.stdout.write("# polygres keys create <name>\n")
    return SUCCESS


def handle_db_info(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.connection_info(project_id)
    database = _database_output(payload)
    output = {"database": database, "request_id": payload.get("request_id")}
    return _emit(
        ctx,
        output,
        [
            ("Project", database.get("project_id")),
            ("Database", database.get("database")),
            ("Username", database.get("username")),
            ("Port", database.get("port")),
            ("Direct host", database.get("direct_host")),
            ("Pooled host", database.get("pooled_host")),
            ("Ready", database.get("ready")),
        ],
    )


def handle_db_psql(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.connection_info(project_id)
    database = _database_output(payload)
    command = [
        "psql",
        "--host",
        str(database["direct_host"]),
        "--port",
        str(database["port"]),
        "--username",
        str(database["username"]),
        "--dbname",
        str(database["database"]),
    ]
    env = {"PGSSLMODE": "require"}
    if shutil.which("psql") is None:
        if ctx.json:
            write_json(
                {
                    "command": command,
                    "env": env,
                    "executed": False,
                    "request_id": payload.get("request_id"),
                }
            )
        elif not ctx.quiet:
            sys.stdout.write("psql is not installed or not on PATH.\n\n")
            sys.stdout.write("Run after installing psql:\n")
            sys.stdout.write("PGSSLMODE=require " + " ".join(command) + "\n")
        return LOCAL_DEPENDENCY
    if ctx.json:
        write_json(
            {
                "command": command,
                "env": env,
                "executed": True,
                "request_id": payload.get("request_id"),
            }
        )
        return SUCCESS
    child_env = dict(os.environ)
    child_env.pop("PGPASSWORD", None)
    child_env.update(env)
    return subprocess.run(command, env=child_env, check=False).returncode


def handle_keys_list(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.list_api_keys(project_id)
    output = {
        "keys": [_sanitize_key(key) for key in _items(payload, "api_keys", "keys")],
        "request_id": payload.get("request_id"),
    }
    if ctx.json:
        write_json(redact(output))
    elif not ctx.quiet:
        print_table(output["keys"], ["id", "name", "prefix", "status"])
    return SUCCESS


def handle_keys_create(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.create_api_key(project_id, args.name)
    key = _normalize_created_key(payload)
    secret = key.get("secret")
    if not isinstance(secret, str) or not secret:
        raise CliError(
            "API_KEY_RESPONSE_INVALID",
            "API key creation response did not include the one-time secret.",
            request_id=payload.get("request_id"),
        )
    output = {"key": key, "request_id": payload.get("request_id")}
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        if sys.stdout.isatty():
            sys.stdout.write("This key is shown once. Store it now.\n\n")
        elif sys.stderr.isatty():
            sys.stderr.write("This key is shown once. Store it now.\n")
        sys.stdout.write(str(key.get("secret", "")) + "\n")
    return SUCCESS


def handle_keys_revoke(ctx: Context, args: argparse.Namespace) -> int:
    _validate_uuid(args.key_id, "key ID")
    project_hint = ctx.args.project or ctx.selected_project_id or "the selected project"
    _require_confirmation(ctx, args.yes, f"Revoke key {args.key_id} for project {project_hint}?")
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.revoke_api_key(project_id, args.key_id)
    key = payload.get("key") or payload.get("api_key") or {"id": args.key_id, "status": "revoked"}
    output = {"key": _sanitize_key(key), "revoked": True, "request_id": payload.get("request_id")}
    return _emit(ctx, output, [("Revoked", args.key_id)])


def handle_migrations_list(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.migrations_list(project_id)
    output = {"migrations": _items(payload, "migrations"), "request_id": payload.get("request_id")}
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        print_table(output["migrations"], ["id", "name", "version", "status"])
    return SUCCESS


def handle_migrations_apply(ctx: Context, args: argparse.Namespace) -> int:
    sql_path = _readable_file(args.file)
    if args.name:
        _validate_migration_name(args.name)
        name = args.name
    else:
        name = _migration_name(sql_path.stem)
    sql_body = _read_text_file(sql_path)
    project_id = _resolve_project_id(ctx, None)
    created = ctx.client.migrations_create(project_id, name, sql_body)
    migration = _object(created, "migration")
    migration_id = migration.get("id")
    _validate_response_uuid(migration_id, "migration")
    applied = ctx.client.migrations_apply(project_id, migration_id)
    applied_migration = applied.get("migration", migration)
    create_operation = (
        created.get("operation") if isinstance(created.get("operation"), dict) else {}
    )
    apply_operation = (
        applied.get("operation") if isinstance(applied.get("operation"), dict) else {}
    )
    status = applied_migration.get("status")
    output = {
        "migration": applied_migration,
        "operation": {
            "created": bool(create_operation.get("created", True)),
            "applied": bool(apply_operation.get("applied", status == "applied")),
            "noop": bool(create_operation.get("noop") or apply_operation.get("noop")),
        },
        "request_id": applied.get("request_id") or created.get("request_id"),
    }
    _emit(
        ctx,
        output,
        [
            ("Migration", output["migration"].get("name", name)),
            ("Status", output["migration"].get("status", "")),
        ],
    )
    return GENERAL_FAILURE if status == "failed" else SUCCESS


def handle_graph_discover(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.graph_discover(project_id)
    return _emit_configuration(
        ctx,
        {
            "configuration": _graph_discovery_configuration(response),
            "request_id": response.get("request_id"),
        },
    )


def handle_graph_config_export(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    return _emit_configuration(ctx, ctx.client.get_graph_configuration(project_id))


def handle_graph_config_apply(ctx: Context, args: argparse.Namespace) -> int:
    payload = _graph_configuration_file(args.file)
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.put_graph_configuration(project_id, payload)
    return _emit_configuration(
        ctx,
        response,
        operation=response.get("operation", {"applied": True}),
    )


def handle_graph_build(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.graph_build(project_id)
    operation = payload.get("operation")
    if not isinstance(operation, dict):
        operation = {
            "build_started": True,
            "build_completed": operation == "completed"
            or payload.get("build_status") == "ready"
            or payload.get("configuration", {}).get("build_status") == "ready",
        }
    output = {
        "graph": payload.get("graph", payload.get("configuration", {})),
        "operation": operation,
        "request_id": payload.get("request_id"),
    }
    return _emit(ctx, output, [("Graph", output["graph"].get("build_status", ""))])


def handle_graph_status(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.graph_status(project_id)
    graph = payload.get("graph") or payload.get("status") or {}
    return _emit(
        ctx,
        {"graph": graph, "request_id": payload.get("request_id")},
        [("Graph", graph.get("build_status", ""))],
    )


def handle_vector_configs_list(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.list_vector_configurations(project_id)
    output = {
        "configurations": _items(payload, "configurations", "vector_configurations"),
        "request_id": payload.get("request_id"),
    }
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        print_table(
            output["configurations"],
            ["id", "name", "index_status", "dimensions", "metric"],
        )
    return SUCCESS


def handle_vector_configs_create(ctx: Context, args: argparse.Namespace) -> int:
    _validate_identifiers(args.schema, args.table, args.row_id_column, args.embedding_column)
    _validate_identifiers(*args.metadata_column, *args.filter_column)
    payload = {
        "name": args.name,
        "schema_name": args.schema,
        "table_name": args.table,
        "row_id_column": args.row_id_column,
        "embedding_column": args.embedding_column,
        "dimensions": args.dimensions,
        "metric": args.metric,
        "metadata_columns": args.metadata_column,
        "filter_columns": args.filter_column,
        "index_kind": args.index_kind,
    }
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.create_vector_configuration(project_id, payload)
    return _emit_config_response(ctx, response)


def handle_vector_configs_delete(ctx: Context, args: argparse.Namespace) -> int:
    _validate_uuid(args.config_id, "configuration ID")
    _require_confirmation(ctx, args.yes, f"Delete vector configuration {args.config_id}?")
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.delete_vector_configuration(project_id, args.config_id)
    return _emit_config_response(ctx, response, default_operation={"deleted": True})


def handle_vector_reindex(ctx: Context, args: argparse.Namespace) -> int:
    _validate_uuid(args.config_id, "configuration ID")
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.reindex_vector_configuration(project_id, args.config_id)
    return _emit_config_response(ctx, response, default_operation={"reindexed": True})


def handle_text_configs_list(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.list_text_configurations(project_id)
    output = {
        "configurations": _items(payload, "configurations", "text_configurations"),
        "request_id": payload.get("request_id"),
    }
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        print_table(output["configurations"], ["id", "name", "search_kind", "index_status"])
    return SUCCESS


def handle_text_create_tsvector(ctx: Context, args: argparse.Namespace) -> int:
    generated_mode = bool(args.text_column or args.generated_column)
    existing_mode = bool(args.tsvector_column)
    if existing_mode == generated_mode:
        raise CliError(
            "VALIDATION_ERROR",
            "Provide exactly one of --tsvector-column or --text-column with --generated-column.",
            exit_code=USAGE,
        )
    _validate_identifiers(args.schema, args.table, args.row_id_column, args.language)
    _validate_identifiers(*args.metadata_column, *args.filter_column)
    migration: dict[str, Any] | None = None
    if existing_mode:
        _validate_identifiers(args.tsvector_column)
        tsvector_column = args.tsvector_column
        generated_column_created = False
    else:
        if not args.text_column or not args.generated_column:
            raise CliError(
                "VALIDATION_ERROR",
                "Generated-column mode requires --text-column and --generated-column.",
                exit_code=USAGE,
            )
        _require_confirmation(ctx, args.yes, "Create a generated tsvector column?")
        _validate_identifiers(args.text_column, args.generated_column)
        tsvector_column = args.generated_column
        migration_name = _migration_name(
            f"m_{args.name}_{args.generated_column}_generated_tsvector"
        )
        sql_body = _generated_tsvector_sql(
            args.schema, args.table, args.text_column, args.generated_column, args.language
        )
        project_id = _resolve_project_id(ctx, None)
        created = ctx.client.migrations_create(project_id, migration_name, sql_body)
        migration_id = _object(created, "migration").get("id")
        _validate_response_uuid(migration_id, "migration")
        applied = ctx.client.migrations_apply(project_id, migration_id)
        migration = applied.get("migration") or created.get("migration")
        if not isinstance(migration, dict) or migration.get("status") == "failed":
            raise CliError(
                "MIGRATION_APPLY_FAILED",
                str(
                    migration.get("error_message")
                    if isinstance(migration, dict)
                    else "Generated-column migration failed."
                ),
                request_id=applied.get("request_id"),
            )
        generated_column_created = True
    if existing_mode:
        project_id = _resolve_project_id(ctx, None)
    payload = {
        "name": args.name,
        "search_kind": "tsvector",
        "schema_name": args.schema,
        "table_name": args.table,
        "row_id_column": args.row_id_column,
        "row_id_columns": [args.row_id_column],
        "tsvector_column": tsvector_column,
        "language": args.language,
        "metadata_columns": args.metadata_column,
        "filter_columns": args.filter_column,
    }
    try:
        response = ctx.client.create_text_configuration(project_id, payload)
    except CliError as exc:
        if migration is not None:
            exc.message = (
                f"{exc.message} The generated column migration was applied; "
                "the generated column may already exist."
            )
        raise
    output = {
        "configuration": response.get("configuration", {}),
        "operation": {
            **(response.get("operation") if isinstance(response.get("operation"), dict) else {}),
            "generated_column_created": generated_column_created,
        },
        "request_id": response.get("request_id"),
    }
    if migration is not None:
        output["migration"] = migration
    return _emit(ctx, output, [("Configuration", output["configuration"].get("id", ""))])


def handle_text_create_fuzzy(ctx: Context, args: argparse.Namespace) -> int:
    _validate_identifiers(
        args.schema, args.table, args.row_id_column, args.text_column, args.language
    )
    _validate_identifiers(*args.metadata_column, *args.filter_column)
    payload = {
        "name": args.name,
        "search_kind": "fuzzy",
        "schema_name": args.schema,
        "table_name": args.table,
        "row_id_column": args.row_id_column,
        "row_id_columns": [args.row_id_column],
        "text_column": args.text_column,
        "language": args.language,
        "metadata_columns": args.metadata_column,
        "filter_columns": args.filter_column,
        "similarity_threshold": args.similarity_threshold,
    }
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.create_text_configuration(project_id, payload)
    return _emit_config_response(ctx, response)


def handle_text_configs_delete(ctx: Context, args: argparse.Namespace) -> int:
    _validate_uuid(args.config_id, "configuration ID")
    _require_confirmation(ctx, args.yes, f"Delete text configuration {args.config_id}?")
    project_id = _resolve_project_id(ctx, None)
    response = ctx.client.delete_text_configuration(project_id, args.config_id)
    return _emit_config_response(ctx, response, default_operation={"deleted": True})


def handle_import_csv(ctx: Context, args: argparse.Namespace) -> int:
    file_path = _readable_file(args.file)
    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        raise CliError(
            "VALIDATION_ERROR", f"Unable to read file: {file_path}", exit_code=USAGE
        ) from exc
    if file_size > MAX_CSV_UPLOAD_BYTES:
        raise CliError(
            "IMPORT_LIMIT_EXCEEDED",
            "CSV file exceeds the 1 GiB local upload limit.",
            exit_code=USAGE,
            details={"limit_bytes": MAX_CSV_UPLOAD_BYTES, "file_size_bytes": file_size},
        )
    _validate_identifiers(args.schema, args.table)
    preview_fields = {
        "target_schema": args.schema,
        "target_table": args.table,
        "mode": args.mode,
        "encoding": args.encoding,
        "has_header": "false" if args.no_header else "true",
        "sample_row_count": "50",
    }
    for cli_name, field_name in [
        ("delimiter", "delimiter"),
        ("quote_char", "quote_char"),
        ("escape_char", "escape_char"),
    ]:
        value = getattr(args, cli_name)
        if value is not None:
            preview_fields[field_name] = value
    project_id = _resolve_project_id(ctx, None)
    preview = ctx.client.csv_preview(project_id, file_path, preview_fields)
    preview_payload = preview.get("preview") if isinstance(preview.get("preview"), dict) else {}
    job_id = preview_payload.get("job_id")
    if not isinstance(job_id, str):
        raise CliError("IMPORT_INVALID", "CSV preview response did not include a job ID.")
    _validate_response_uuid(job_id, "import preview job")
    columns = preview_payload.get("columns")
    if not isinstance(columns, list):
        raise CliError("IMPORT_INVALID", "CSV preview response did not include columns.")
    import_fields = {
        "target_schema": args.schema,
        "target_table": args.table,
        "mode": args.mode,
        "encoding": str(preview_payload.get("encoding", args.encoding)),
        "delimiter": str(preview_payload.get("delimiter", args.delimiter or "")),
        "quote_char": str(preview_payload.get("quote_char", args.quote_char or '"')),
        "has_header": (
            "true" if preview_payload.get("has_header", not args.no_header) else "false"
        ),
    }
    effective_escape = preview_payload.get("escape_char", args.escape_char)
    if effective_escape is not None:
        import_fields["escape_char"] = str(effective_escape)
    import_fields.update({"job_id": job_id, "columns": json.dumps(columns)})
    started = ctx.client.start_csv_import(project_id, file_path, import_fields)
    output = _import_output(started)
    if args.wait and output["import"].get("status") not in {"succeeded", "failed", "cancelled"}:
        import_id = output["import"].get("id")
        _validate_response_uuid(import_id, "import job")
        output = _poll_import(ctx, project_id, import_id, args.timeout, started)
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        _write_import_human(output)
    return _import_exit_code(output["import"])


def handle_import_status(ctx: Context, args: argparse.Namespace) -> int:
    if args.job_id:
        _validate_uuid(args.job_id, "import job ID")
    project_id = _resolve_project_id(ctx, None)
    if args.job_id:
        job_id = args.job_id
    else:
        imports_payload = ctx.client.list_imports(project_id)
        imports = _items(imports_payload, "imports")
        job_id = _latest_import_id(imports)
        if job_id is None:
            output = {
                "imports": imports,
                "latest_import": None,
                "request_id": imports_payload.get("request_id"),
            }
            if ctx.json:
                write_json(output)
            elif not ctx.quiet:
                sys.stdout.write("No imports found.\n")
                if output.get("request_id"):
                    sys.stdout.write(f"Request ID: {output['request_id']}\n")
            return SUCCESS
    _validate_uuid(job_id, "import job ID")
    output = _import_output(ctx.client.get_import(project_id, job_id))
    if ctx.json:
        write_json(output)
    elif not ctx.quiet:
        _write_import_human(output)
    return _import_exit_code(output["import"])


def handle_ready(ctx: Context, args: argparse.Namespace) -> int:
    project_id = _resolve_project_id(ctx, None)
    payload = ctx.client.retrieval_readiness(project_id)
    output = {
        "project_id": payload.get("project_id", project_id),
        "graph": payload.get("graph", {}),
        "vector": payload.get("vector", {}),
        "hybrid": payload.get("hybrid", {}),
        "request_id": payload.get("request_id"),
    }
    return _emit(
        ctx,
        output,
        [
            ("Project", output["project_id"]),
            ("Graph ready", output["graph"].get("ready", "")),
            ("Vector ready", output["vector"].get("ready", "")),
            ("Hybrid ready", output["hybrid"].get("ready", "")),
        ],
    )


def handle_config_path(ctx: Context, args: argparse.Namespace) -> int:
    payload = {"path": str(ctx.store.path)}
    if ctx.json:
        write_json(payload)
    elif not ctx.quiet:
        sys.stdout.write(payload["path"] + "\n")
    return SUCCESS


def _resolve_project_id(ctx: Context, positional: str | None) -> str:
    candidate = positional or ctx.args.project or ctx.selected_project_id
    if not candidate:
        raise CliError(
            "PROJECT_REQUIRED",
            "Select a project with `polygres projects use <project>` or pass --project.",
            exit_code=USAGE,
        )
    if PROJECT_ID_RE.match(candidate):
        return candidate
    project = _resolve_project(ctx, candidate)
    return _project_api_id(project)


def _resolve_project(ctx: Context, candidate: str) -> dict[str, Any]:
    if PROJECT_ID_RE.match(candidate):
        payload = ctx.client.get_project(candidate)
        project = _object(payload, "project")
        project.setdefault("external_id", candidate)
        project.setdefault("id", candidate)
        project["request_id"] = payload.get("request_id")
        return project
    projects_payload = ctx.client.list_projects()
    matches = [
        project
        for project in _items(projects_payload, "projects")
        if project.get("name") == candidate
    ]
    if not matches:
        raise CliError(
            "PROJECT_NOT_FOUND",
            "Project not found.",
            exit_code=NOT_FOUND,
            details={"project": candidate},
            request_id=projects_payload.get("request_id"),
        )
    if len(matches) > 1:
        raise CliError(
            "PROJECT_AMBIGUOUS",
            "Project name matches more than one project.",
            exit_code=CONFLICT,
            details={
                "project": candidate,
                "matches": [_project_api_id(project) for project in matches],
            },
        )
    project = dict(matches[0])
    project["request_id"] = projects_payload.get("request_id")
    return project


def _project_api_id(project: dict[str, Any]) -> str:
    value = project.get("external_id") or project.get("id")
    if isinstance(value, str) and value:
        return value
    raise CliError("PROJECT_INVALID", "Project response did not include an ID.")


def _has_external_ids(projects: list[dict[str, Any]]) -> bool:
    return any(isinstance(project.get("external_id"), str) for project in projects)


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return dict(value)
    return {k: v for k, v in payload.items() if k != "request_id"}


def _sanitize_key(key: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in key.items() if k not in {"raw_key", "secret", "api_key"}}


def _normalize_created_key(payload: dict[str, Any]) -> dict[str, Any]:
    key = dict(payload.get("key") or payload.get("api_key") or {})
    if "raw_key" in key:
        key["secret"] = key.pop("raw_key")
    return key


def _database_output(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": payload.get("project_id"),
        "database": payload.get("database"),
        "username": payload.get("username"),
        "port": payload.get("port"),
        "direct_host": payload.get("direct", {}).get("host"),
        "pooled_host": payload.get("pooled", {}).get("host"),
        "ready": payload.get("ready", payload.get("readiness")),
    }


def _emit(ctx: Context, payload: dict[str, Any], human_items: list[tuple[str, Any]]) -> int:
    if ctx.json:
        write_json(redact(payload, allow_key_secret="key" in payload))
    elif not ctx.quiet:
        print_kv(human_items)
    return SUCCESS


def _emit_configuration(
    ctx: Context, payload: dict[str, Any], *, operation: dict[str, Any] | None = None
) -> int:
    output = {
        "configuration": payload.get("configuration", payload.get("graph_configuration", {})),
        "request_id": payload.get("request_id"),
    }
    if operation is not None:
        output["operation"] = operation
    if ctx.json:
        write_json(redact(output))
    elif not ctx.quiet:
        sys.stdout.write(json.dumps(output["configuration"], indent=2, sort_keys=True) + "\n")
    return SUCCESS


def _emit_config_response(
    ctx: Context, payload: dict[str, Any], *, default_operation: dict[str, Any] | None = None
) -> int:
    output = {
        "configuration": payload.get("configuration", {}),
        "request_id": payload.get("request_id"),
    }
    operation = (
        payload.get("operation")
        if isinstance(payload.get("operation"), dict)
        else default_operation
    )
    if operation is not None:
        output["operation"] = operation
    return _emit(ctx, output, [("Configuration", output["configuration"].get("id", ""))])


def _project_status_output(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    resources = payload.get("resources") if isinstance(payload.get("resources"), dict) else {}
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}

    if status:
        if not project:
            project = {
                "id": project_id,
                "status": status.get("project") or status.get("status"),
            }
        if not runtime:
            runtime = {
                key: status[key]
                for key in (
                    "database",
                    "direct_host",
                    "effective_tier_id",
                    "namespace",
                    "pooled_host",
                    "pooler",
                    "runtime_api",
                    "runtime_api_host",
                    "runtime_api_url",
                    "runtime_sync",
                    "traefik",
                )
                if key in status
            }
        if not resources:
            resources = {
                key: status[key]
                for key in ("last_storage_measurement", "memory")
                if key in status
            }
        if not readiness:
            readiness = {
                key: status[key]
                for key in ("graph", "hybrid", "text", "vector")
                if key in status
            }

    output = {
        "project": project,
        "runtime": runtime,
        "resources": resources,
        "readiness": readiness,
        "request_id": payload.get("request_id"),
    }
    return output


def _poll_project_status(
    ctx: Context, project_id: str, *, deadline: float
) -> dict[str, Any]:
    last_status: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        payload = ctx.client.get_project_status(project_id, deadline=deadline)
        status = (
            payload.get("status")
            if isinstance(payload.get("status"), dict)
            else payload.get("project", {})
        )
        last_status = status if isinstance(status, dict) else {}
        project_status = last_status.get("project") or last_status.get("status")
        if project_status in {"ready", "read_only"}:
            return last_status
        if project_status == "failed":
            raise CliError("PROJECT_PROVISIONING_FAILED", "Project provisioning failed.")
        if project_status in {"suspended", "deleting"}:
            raise CliError(
                "PROJECT_UNAVAILABLE", f"Project is {project_status}.", exit_code=CONFLICT
            )
        if project_status == "deleted":
            raise CliError("PROJECT_NOT_FOUND", "Project was deleted.", exit_code=NOT_FOUND)
        _write_poll_progress(ctx, "Project", project_id, last_status)
        _sleep_until_deadline(_poll_interval(payload), deadline)
    raise CliError(
        "TIMEOUT",
        f"Timed out waiting for project {project_id}; last status is still in progress.",
        exit_code=UNAVAILABLE,
        details={"status": last_status},
    )


def _project_create_wait_error(
    *,
    project: dict[str, Any],
    project_id: str,
    create_request_id: object,
    cause: CliError,
) -> CliError:
    if cause.code not in {"SERVICE_UNAVAILABLE", "TIMEOUT"} and cause.exit_code != UNAVAILABLE:
        return cause
    project_name = project.get("name")
    project_status = project.get("status")
    details: dict[str, Any] = {
        "project": {
            "id": project.get("id"),
            "external_id": project.get("external_id") or project_id,
            "name": project_name,
            "status": project_status,
        },
        "create_request_id": create_request_id,
        "wait_error": {
            "code": cause.code,
            "message": cause.message,
            "details": cause.details,
            "request_id": cause.request_id,
        },
    }
    details["project"] = {
        key: value for key, value in details["project"].items() if value is not None
    }
    code = (
        "PROJECT_READINESS_TIMEOUT"
        if cause.code == "TIMEOUT"
        else "PROJECT_READINESS_UNAVAILABLE"
    )
    message = (
        f"Project {project_id} was created"
        " but readiness polling timed out."
        if cause.code == "TIMEOUT"
        else f"Project {project_id} was created but readiness polling failed."
    )
    message += f" Run `polygres projects status {project_id}` to resume validation or cleanup."
    return CliError(
        code,
        message,
        exit_code=UNAVAILABLE,
        details=details,
        request_id=cause.request_id or str(create_request_id or ""),
    )


def _poll_import(
    ctx: Context, project_id: str, job_id: str, timeout_seconds: int, previous: dict[str, Any]
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    interval_payload = previous
    last_output = _import_output(previous)
    last_progress: str | None = None
    while time.monotonic() < deadline:
        progress = json.dumps(last_output["import"], sort_keys=True, default=str)
        if progress != last_progress:
            _write_poll_progress(ctx, "Import", job_id, last_output["import"])
            last_progress = progress
        _sleep_until_deadline(_poll_interval(interval_payload), deadline)
        if time.monotonic() >= deadline:
            break
        payload = ctx.client.get_import(project_id, job_id, deadline=deadline)
        output = _import_output(payload)
        if output["import"].get("status") in {"succeeded", "failed", "cancelled"}:
            return output
        interval_payload = payload
        last_output = output
    raise CliError(
        "TIMEOUT",
        f"Timed out waiting for import {job_id}; it is still in progress.",
        exit_code=UNAVAILABLE,
        details={"import": last_output["import"], "request_id": last_output.get("request_id")},
    )


def _poll_interval(payload: dict[str, Any]) -> int:
    value = payload.get("poll_interval_seconds", 2)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 2
    return min(max(seconds, 1), 30)


def _import_output(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("import") or payload.get("job") or {}
    return {"import": item, "request_id": payload.get("request_id")}


def _write_import_human(output: dict[str, Any]) -> None:
    item = output["import"]
    sys.stdout.write(f"Import {item.get('id')} {item.get('status')}\n")
    request_id = output.get("request_id")
    if request_id:
        sys.stdout.write(f"Request ID: {request_id}\n")
    if item.get("status") != "failed":
        return
    error = item.get("error") if isinstance(item.get("error"), dict) else {}
    error_code = item.get("error_code") or error.get("code")
    error_message = item.get("error_message") or error.get("message")
    if error_code:
        sys.stdout.write(f"Error code: {error_code}\n")
    if error_message:
        sys.stdout.write(f"Error message: {error_message}\n")
    for label, key in [
        ("Row errors", "row_errors"),
        ("Row details", "row_details"),
        ("Details", "details"),
        ("Errors", "errors"),
    ]:
        value = item.get(key)
        if value:
            sys.stdout.write(f"{label}: {json.dumps(value, sort_keys=True)}\n")
    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    for label, key in [
        ("SQL state", "sqlstate"),
        ("Detail", "detail"),
        ("Progress row errors", "row_errors"),
        ("Progress details", "details"),
    ]:
        value = progress.get(key)
        if value:
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, sort_keys=True)
            else:
                rendered = str(value)
            sys.stdout.write(f"{label}: {rendered}\n")


def _import_exit_code(item: dict[str, Any]) -> int:
    status = item.get("status")
    if status == "succeeded":
        return SUCCESS
    if status == "failed":
        return GENERAL_FAILURE
    if status == "cancelled":
        return CONFLICT
    return SUCCESS


def _latest_import_id(imports: list[dict[str, Any]]) -> str | None:
    if not imports:
        return None
    imports.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    job_id = imports[0].get("id")
    if not isinstance(job_id, str):
        raise CliError("IMPORT_INVALID", "Latest import did not include an ID.")
    return job_id


def _require_confirmation(ctx: Context, yes: bool, prompt: str) -> None:
    if yes:
        return
    if sys.stdin.isatty():
        sys.stderr.write(prompt + " Type 'yes' to continue: ")
        if sys.stdin.readline().strip() == "yes":
            return
    raise CliError(
        "CONFIRMATION_REQUIRED",
        "Re-run with --yes to confirm.",
        exit_code=USAGE,
    )


def _readable_file(value: str) -> Path:
    path = Path(value)
    if not path.exists() or not path.is_file():
        raise CliError(
            "VALIDATION_ERROR",
            f"File does not exist or is not a regular file: {path}",
            exit_code=USAGE,
        )
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise CliError(
            "VALIDATION_ERROR", f"File is not readable: {path}", exit_code=USAGE
        ) from exc
    return path


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CliError(
            "VALIDATION_ERROR",
            f"File is not readable UTF-8 text: {path}",
            exit_code=USAGE,
        ) from exc


def _json_object_file(value: str) -> dict[str, Any]:
    path = _readable_file(value)
    try:
        payload = json.loads(_read_text_file(path))
    except json.JSONDecodeError as exc:
        raise CliError("VALIDATION_ERROR", f"Invalid JSON file: {path}", exit_code=USAGE) from exc
    if not isinstance(payload, dict):
        raise CliError("VALIDATION_ERROR", "JSON file must contain an object.", exit_code=USAGE)
    return payload


def _graph_configuration_file(value: str) -> dict[str, Any]:
    payload = _json_object_file(value)
    if "configuration" not in payload:
        configuration = payload
    else:
        allowed_wrapper_keys = {"configuration", "request_id"}
        extra_keys = sorted(set(payload) - allowed_wrapper_keys)
        if extra_keys:
            raise CliError(
                "VALIDATION_ERROR",
                "Graph configuration export contains unsupported wrapper fields.",
                exit_code=USAGE,
                details={"fields": extra_keys},
            )
        configuration = payload["configuration"]
        if configuration is None:
            raise CliError(
                "GRAPH_CONFIGURATION_EMPTY",
                "Graph configuration export does not contain an applyable configuration.",
                exit_code=USAGE,
            )
        if not isinstance(configuration, dict):
            raise CliError(
                "VALIDATION_ERROR",
                "Graph configuration must contain an object.",
                exit_code=USAGE,
            )
    _reject_unknown_fields(
        configuration,
        GRAPH_CONFIGURATION_KEYS | GRAPH_CONFIGURATION_READ_ONLY_KEYS,
        "graph configuration",
    )
    request_configuration = {
        key: configuration[key]
        for key in GRAPH_CONFIGURATION_KEYS
        if key in configuration
    }
    _validate_graph_configuration(request_configuration)
    return request_configuration


def _graph_discovery_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    node_tables = payload.get("node_tables", [])
    relationships = payload.get("relationships", [])
    filter_columns = payload.get("filter_columns", [])
    if not all(isinstance(value, list) for value in (node_tables, relationships, filter_columns)):
        raise CliError(
            "GRAPH_DISCOVERY_INVALID",
            "Graph discovery response contains invalid candidate arrays.",
        )
    configuration = {
        "registered_tables": [
            {
                key: item[key]
                for key in (
                    "schema",
                    "table",
                    "id_column",
                    "id_columns",
                    "columns",
                    "tenant_column",
                )
                if key in item
            }
            if isinstance(item, dict)
            else item
            for item in node_tables
        ],
        "registered_relationships": [
            {
                key: item[key]
                for key in (
                    "from_schema",
                    "from_table",
                    "from_column",
                    "to_schema",
                    "to_table",
                    "to_column",
                    "label",
                    "bidirectional",
                )
                if key in item
            }
            if isinstance(item, dict)
            else item
            for item in relationships
        ],
        "filter_columns": [
            {key: item[key] for key in ("schema", "table", "column", "type") if key in item}
            if isinstance(item, dict)
            else item
            for item in filter_columns
        ],
        "runtime_settings": {},
    }
    try:
        _validate_graph_configuration(configuration)
    except CliError as exc:
        raise CliError(
            "GRAPH_DISCOVERY_INVALID",
            f"Graph discovery response is not applyable: {exc.message}",
            details=exc.details,
        ) from exc
    return configuration


def _validate_graph_configuration(configuration: dict[str, Any]) -> None:
    _reject_unknown_fields(configuration, GRAPH_CONFIGURATION_KEYS, "graph configuration")
    for key in ("registered_tables", "registered_relationships", "filter_columns"):
        value = configuration.get(key, [])
        if not isinstance(value, list):
            _graph_invalid(f"{key} must be an array.")
    runtime_settings = configuration.get("runtime_settings", {})
    if not isinstance(runtime_settings, dict):
        _graph_invalid("runtime_settings must be an object.")

    table_keys = {"schema", "table", "id_column", "id_columns", "columns", "tenant_column"}
    for index, item in enumerate(configuration.get("registered_tables", [])):
        if not isinstance(item, dict):
            _graph_invalid(f"registered_tables[{index}] must be an object.")
        _reject_unknown_fields(item, table_keys, f"registered_tables[{index}]")
        _required_string(item, "table", f"registered_tables[{index}]")
        for key in ("schema", "id_column", "tenant_column"):
            if key in item and item[key] is not None:
                _graph_identifier(item[key], f"registered_tables[{index}].{key}")
        for key in ("id_columns", "columns"):
            values = item.get(key, [])
            if not isinstance(values, list):
                _graph_invalid(f"registered_tables[{index}].{key} must be an array.")
            for value in values:
                _graph_identifier(value, f"registered_tables[{index}].{key}")
        has_single = isinstance(item.get("id_column"), str) and bool(item["id_column"])
        has_multiple = bool(item.get("id_columns"))
        if has_single == has_multiple:
            _graph_invalid(
                f"registered_tables[{index}] must use exactly one of id_column or id_columns."
            )

    relationship_keys = {
        "from_schema",
        "from_table",
        "from_column",
        "to_schema",
        "to_table",
        "to_column",
        "label",
        "bidirectional",
    }
    for index, item in enumerate(configuration.get("registered_relationships", [])):
        if not isinstance(item, dict):
            _graph_invalid(f"registered_relationships[{index}] must be an object.")
        _reject_unknown_fields(item, relationship_keys, f"registered_relationships[{index}]")
        for key in ("from_table", "from_column", "to_table", "to_column", "label"):
            _required_string(item, key, f"registered_relationships[{index}]")
        for key in ("from_schema", "to_schema"):
            if key in item:
                _graph_identifier(item[key], f"registered_relationships[{index}].{key}")
        if "bidirectional" in item and not isinstance(item["bidirectional"], bool):
            _graph_invalid(f"registered_relationships[{index}].bidirectional must be boolean.")

    filter_keys = {"schema", "table", "column", "type"}
    filter_types = {"numeric", "boolean", "text", "date", "timestamptz", "uuid"}
    for index, item in enumerate(configuration.get("filter_columns", [])):
        if not isinstance(item, dict):
            _graph_invalid(f"filter_columns[{index}] must be an object.")
        _reject_unknown_fields(item, filter_keys, f"filter_columns[{index}]")
        for key in ("table", "column"):
            _required_string(item, key, f"filter_columns[{index}]")
        if "schema" in item:
            _graph_identifier(item["schema"], f"filter_columns[{index}].schema")
        if item.get("type") not in filter_types:
            _graph_invalid(f"filter_columns[{index}].type is invalid.")


def _reject_unknown_fields(item: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        _graph_invalid(f"{label} contains unknown fields.", {"fields": unknown})


def _required_string(item: dict[str, Any], key: str, label: str) -> None:
    if key not in item:
        _graph_invalid(f"{label}.{key} is required.")
    _graph_identifier(item[key], f"{label}.{key}")


def _graph_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not SQL_IDENTIFIER_RE.fullmatch(value):
        _graph_invalid(f"{label} must be a valid SQL identifier.")


def _graph_invalid(message: str, details: dict[str, Any] | None = None) -> None:
    raise CliError(
        "GRAPH_CONFIGURATION_INVALID",
        message,
        exit_code=USAGE,
        details=details or {},
    )


def _validate_uuid(value: str, label: str) -> None:
    if not UUID_LIKE_RE.match(value):
        raise CliError("VALIDATION_ERROR", f"Invalid {label}.", exit_code=USAGE)


def _validate_response_uuid(value: object, label: str) -> None:
    if not isinstance(value, str) or not UUID_LIKE_RE.fullmatch(value):
        raise CliError(
            "MIGRATION_INVALID" if "migration" in label else "IMPORT_INVALID",
            f"{label.capitalize()} response did not include a valid ID.",
        )


def _validate_migration_name(value: str) -> None:
    if not MIGRATION_NAME_RE.match(value):
        raise CliError("VALIDATION_ERROR", "Invalid migration name.", exit_code=USAGE)


def _validate_identifiers(*values: str | None) -> None:
    for value in values:
        if value is not None and not SQL_IDENTIFIER_RE.match(value):
            raise CliError("VALIDATION_ERROR", f"Invalid SQL identifier: {value}", exit_code=USAGE)


def _generated_tsvector_sql(
    schema: str, table: str, source_column: str, generated_column: str, language: str
) -> str:
    return (
        f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS "{generated_column}" '
        "tsvector GENERATED ALWAYS AS "
        f"(to_tsvector('{language}'::regconfig, coalesce(\"{source_column}\"::text, ''))) STORED;"
    )


def _migration_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "migration"
    if not re.match(r"^[A-Za-z_]", normalized):
        normalized = f"m_{normalized}"
    _validate_migration_name(normalized)
    return normalized


def _timeout_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if seconds < 1 or seconds > 86400:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 86400")
    return seconds


def _dimensions(value: str) -> int:
    try:
        dimensions = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dimensions must be an integer") from exc
    if dimensions < 1 or dimensions > 2000:
        raise argparse.ArgumentTypeError("dimensions must be between 1 and 2000")
    return dimensions


def _similarity_threshold(value: str) -> float:
    try:
        threshold = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("similarity threshold must be a number") from exc
    if threshold < 0 or threshold > 1:
        raise argparse.ArgumentTypeError("similarity threshold must be between 0 and 1")
    return threshold


def _one_char(value: str) -> str:
    if len(value) != 1:
        raise argparse.ArgumentTypeError("value must be one character")
    return value


def _delimiter(value: str) -> str:
    value = _one_char(value)
    if value not in {",", "\t", ";", "|"}:
        raise argparse.ArgumentTypeError("delimiter must be comma, tab, semicolon, or pipe")
    return value


def _remove_pgbouncer_query(value: object) -> object:
    value = _passwordless_url(value)
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not (key.lower() == "pgbouncer" and item.lower() == "true")
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _passwordless_url(value: object) -> object:
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if parsed.username is None:
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"{parsed.username}@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _summary_value(value: object) -> str:
    if not isinstance(value, dict):
        return str(value or "")
    for key in ("status", "ready", "runtime_status", "project"):
        if key in value:
            return str(value[key])
    return json.dumps(value, sort_keys=True) if value else ""


def _resource_pressure(resources: dict[str, Any]) -> str:
    for key in ("pressure", "resource_pressure", "memory_pressure", "status"):
        if key in resources:
            return str(resources[key])
    memory = resources.get("memory")
    if isinstance(memory, dict):
        return _resource_pressure(memory)
    return _summary_value(resources)


def _sleep_until_deadline(seconds: float, deadline: float) -> None:
    remaining = max(deadline - time.monotonic(), 0.0)
    delay = min(float(seconds), remaining)
    if delay > 0:
        time.sleep(delay)


def _write_poll_progress(
    ctx: Context, operation: str, identifier: str, status: dict[str, Any]
) -> None:
    if ctx.json or ctx.quiet:
        return
    state = status.get("status") or status.get("project") or "in progress"
    progress = status.get("progress")
    suffix = f" {json.dumps(progress, sort_keys=True)}" if isinstance(progress, dict) else ""
    sys.stderr.write(f"{operation} {identifier}: {state}{suffix}\n")
