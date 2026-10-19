"""Offline tests for the live Meta/Instagram credential smoke check."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_meta_integration  # noqa: E402


class Response:
    """Minimal context-managed response returned by the injected opener."""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class Recorder:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return Response(self.payload)


def test_valid_token_reads_the_expected_instagram_profile():
    opener = Recorder({
        "user_id": "17841400000000000",
        "username": "BostonCafeBikers",
    })

    assert check_meta_integration.check_integration(
        "secret-token", opener=opener
    ) == "bostoncafebikers"
    assert opener.request.full_url == (
        "https://graph.instagram.com/v26.0/me?fields=user_id%2Cusername"
    )
    assert opener.request.get_header("Authorization") == "Bearer secret-token"
    assert "secret-token" not in opener.request.full_url
    assert opener.timeout == check_meta_integration.TIMEOUT


def test_a_valid_token_for_the_wrong_account_fails():
    opener = Recorder({"user_id": "123", "username": "some_other_account"})

    with pytest.raises(check_meta_integration.MetaIntegrationError, match="not the expected"):
        check_meta_integration.check_integration("secret-token", opener=opener)


def test_response_must_include_an_id_and_username():
    for payload in ({"username": "bostoncafebikers"}, {"user_id": "123"}, []):
        opener = Recorder(payload)
        with pytest.raises(check_meta_integration.MetaIntegrationError):
            check_meta_integration.check_integration("secret-token", opener=opener)


def test_meta_error_is_useful_but_never_leaks_the_token():
    token = "top-secret-token"
    body = json.dumps({
        "error": {
            "message": f"Error validating access token {token}",
            "type": "OAuthException",
            "code": 190,
        }
    }).encode("utf-8")

    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )

    with pytest.raises(check_meta_integration.MetaIntegrationError) as excinfo:
        check_meta_integration.check_integration(token, opener=rejected)
    message = str(excinfo.value)
    assert "HTTP 400" in message
    assert "OAuthException" in message
    assert "code 190" in message
    assert token not in message
    assert "[redacted]" in message


def test_token_is_required_before_any_request_is_made():
    def must_not_open(*_args, **_kwargs):
        raise AssertionError("the empty-token path must stay offline")

    with pytest.raises(check_meta_integration.MetaIntegrationError, match="missing"):
        check_meta_integration.check_integration("   ", opener=must_not_open)


def test_main_reports_success_without_printing_the_token(monkeypatch, capsys):
    token = "top-secret-token"
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", token)
    monkeypatch.setattr(
        check_meta_integration,
        "fetch_profile",
        lambda _token, opener=None: {
            "user_id": "123",
            "username": "bostoncafebikers",
        },
    )

    assert check_meta_integration.main() == 0
    output = capsys.readouterr()
    assert "passed for @bostoncafebikers" in output.out
    assert token not in output.out + output.err
