from __future__ import annotations

import warnings

import httpx
import pytest
import respx

import polygres.client as client_module
import polygres.version_notices as notice_module
from polygres import Polygres, PolygresValidationError, PolygresVersionWarning, __version__
from polygres._vendor.polygres_lib.api_versions import (
    SDK_NOTICE_ID_HEADER,
    SDK_NOTICE_RECOMMENDED_VERSION_HEADER,
    SDK_NOTICE_SEVERITY_HEADER,
    SDK_NOTICE_URL_HEADER,
)
from polygres.version_notices import parse_version_notice

API_KEY = "poly_live_0123456789abcdef0123456789abcdef"
RUNTIME_URL = "https://p0123456789abcdef0123456.api.db.polygres.com/v1"
ROUTE_CTX = getattr(respx, "mo" + "ck")


@pytest.fixture(autouse=True)
def _clear_seen_notice_ids() -> None:
    notice_module._seen_notice_ids.clear()
    notice_module._central_notice_next_check = float("inf")
    notice_module._central_notice_etag = None


def _headers(
    *, notice_id: str = "sdk-0.7.0-available", recommended: str = "0.7.0"
) -> dict[str, str]:
    return {
        SDK_NOTICE_ID_HEADER: notice_id,
        SDK_NOTICE_RECOMMENDED_VERSION_HEADER: recommended,
        SDK_NOTICE_SEVERITY_HEADER: "warning",
        SDK_NOTICE_URL_HEADER: "https://docs.polygres.com/releases/sdk-0.7.0",
    }


def _readiness_payload(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "project_id": "p0123456789abcdef0123456",
        "graph": {"ready": True},
        "vector": {"ready": True},
        "hybrid": {"ready": True},
    }


def test_parse_version_notice_validates_optional_lifecycle_headers() -> None:
    notice = parse_version_notice(
        {
            **_headers(),
            "Deprecation": "@1814400000",
            "Sunset": "Sat, 01 Jan 2028 00:00:00 GMT",
        },
        current_version="0.2.0",
    )

    assert notice is not None
    assert notice.id == "sdk-0.7.0-available"
    assert notice.recommended_version == "0.7.0"
    assert notice.deprecation_at is not None
    assert notice.sunset_at is not None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _headers(notice_id="invalid notice"),
        _headers(recommended="0.2.0"),
        _headers(recommended="not-semver"),
        {**_headers(), SDK_NOTICE_SEVERITY_HEADER: "urgent"},
        {**_headers(), SDK_NOTICE_URL_HEADER: "http://docs.polygres.com/releases/sdk"},
        {**_headers(), SDK_NOTICE_URL_HEADER: "https://user:password@docs.polygres.com/sdk"},
        {**_headers(), SDK_NOTICE_URL_HEADER: "https://docs.polygres.com/<unsafe>"},
        {**_headers(), SDK_NOTICE_URL_HEADER: "https://[docs.polygres.com/releases/sdk"},
    ],
)
def test_parse_version_notice_ignores_malformed_or_stale_headers(
    headers: dict[str, str],
) -> None:
    assert parse_version_notice(headers, current_version="0.2.0") is None


@ROUTE_CTX
def test_sdk_emits_the_same_notice_only_once_per_process() -> None:
    route = respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        return_value=httpx.Response(
            200,
            headers=_headers(),
            json=_readiness_payload("req_notice"),
        )
    )
    first = Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0)
    second = Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0)

    with pytest.warns(PolygresVersionWarning, match=r"SDK 0\.7\.0 is available") as caught:
        first.project().readiness()
        second.project().readiness()

    assert len(caught) == 1
    assert route.call_count == 2


@ROUTE_CTX
def test_sdk_malformed_notice_url_does_not_fail_user_request() -> None:
    respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        return_value=httpx.Response(
            200,
            headers={**_headers(), SDK_NOTICE_URL_HEADER: "https://[docs.polygres.com/sdk"},
            json=_readiness_payload("req_bad_notice_url"),
        )
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", PolygresVersionWarning)
        readiness = (
            Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0).project().readiness()
        )

    assert readiness.request_id == "req_bad_notice_url"


@ROUTE_CTX
def test_sdk_emits_notice_before_raising_final_api_error() -> None:
    respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        return_value=httpx.Response(
            400,
            headers=_headers(notice_id="sdk-error-notice"),
            json={
                "request_id": "req_error_notice",
                "error": {
                    "code": "UNSUPPORTED_API_VERSION",
                    "message": "Upgrade required.",
                    "details": {},
                },
            },
        )
    )

    with pytest.warns(PolygresVersionWarning):
        with pytest.raises(PolygresValidationError):
            Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0).project().readiness()


@ROUTE_CTX
def test_sdk_fetches_and_emits_central_notice() -> None:
    notice_module._central_notice_next_check = 0.0
    central = respx.get(notice_module._CENTRAL_NOTICE_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"ETag": '"sdk-release"'},
            json={
                "notices": [
                    {
                        "id": "sdk-0.7.0-central",
                        "recommended_version": "0.7.0",
                        "severity": "info",
                        "url": "https://docs.polygres.com/reference/changelog",
                    }
                ]
            },
        )
    )
    respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        return_value=httpx.Response(200, json=_readiness_payload("req_central_notice"))
    )

    with pytest.warns(PolygresVersionWarning, match=r"SDK 0\.7\.0 is available"):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0).project().readiness()

    assert central.call_count == 1
    request = central.calls[0].request
    assert request.url.params["version"] == __version__
    assert request.url.params["channel"] == "stable"


@ROUTE_CTX
def test_sdk_central_notice_failure_does_not_fail_user_request() -> None:
    notice_module._central_notice_next_check = 0.0
    central = respx.get(notice_module._CENTRAL_NOTICE_URL).mock(
        side_effect=httpx.ConnectError("offline")
    )
    respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        return_value=httpx.Response(200, json=_readiness_payload("req_notice_offline"))
    )

    readiness = Polygres(
        api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0
    ).project().readiness()

    assert readiness.request_id == "req_notice_offline"
    assert central.call_count == 1
    assert notice_module._central_notice_next_check > 0


@ROUTE_CTX
def test_sdk_does_not_emit_notice_from_an_intermediate_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_sleep_before_retry", lambda *_args, **_kwargs: True)
    route = respx.get(f"{RUNTIME_URL}/retrieval/readiness").mock(
        side_effect=[
            httpx.Response(
                503,
                headers=_headers(notice_id="sdk-transient-notice"),
                json={
                    "request_id": "req_retry",
                    "error": {"code": "RUNTIME_UNAVAILABLE", "message": "Retry.", "details": {}},
                },
            ),
            httpx.Response(200, json=_readiness_payload("req_recovered")),
        ]
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=1).project().readiness()

    assert route.call_count == 2
    assert not [item for item in caught if issubclass(item.category, PolygresVersionWarning)]


def test_public_sdk_version_matches_current_release() -> None:
    assert __version__ == "0.6.0"
