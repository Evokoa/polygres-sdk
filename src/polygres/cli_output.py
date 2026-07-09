from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from polygres.cli_errors import CliError
from polygres.cli_secrets import redact


def write_json(payload: dict[str, Any], *, stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, sort_keys=True) + "\n")


def write_error(error: CliError, *, json_output: bool) -> None:
    payload: dict[str, Any] = {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        }
    }
    if error.request_id:
        payload["request_id"] = error.request_id
    if json_output:
        write_json(redact(payload), stream=sys.stdout)
    else:
        message = error.message
        if error.request_id:
            message = f"{message} (request_id: {error.request_id})"
        sys.stderr.write(message + "\n")


def print_kv(items: list[tuple[str, Any]]) -> None:
    width = max((len(label) for label, _ in items), default=0)
    for label, value in items:
        sys.stdout.write(f"{label.ljust(width)}  {value}\n")


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        sys.stdout.write("No results.\n")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    sys.stdout.write("  ".join(column.ljust(widths[column]) for column in columns) + "\n")
    for row in rows:
        sys.stdout.write(
            "  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns)
            + "\n"
        )
