#!/usr/bin/env python3
"""Smoke-test the repository's Meta/Instagram credential.

This is deliberately separate from the offline test suite: it makes one live,
read-only request to Meta and is run only by the manually dispatched
``meta-integration.yml`` workflow.  The access token is read from the
environment and sent in an Authorization header, never in the URL or output.

The request targets Instagram API with Instagram Login.  A valid credential
must be able to read the connected professional account's ``user_id`` and
``username``; the username is also checked so a valid token for the wrong
account cannot make the smoke test pass.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH_API_VERSION = "v26.0"
GRAPH_API_URL = f"https://graph.instagram.com/{GRAPH_API_VERSION}/me"
EXPECTED_USERNAME = "bostoncafebikers"
TIMEOUT = 20
USER_AGENT = "boscafebikers-meta-check/1.0"
MAX_ERROR_BYTES = 64 * 1024


class MetaIntegrationError(RuntimeError):
    """The credential or Meta response did not satisfy the smoke test."""


def profile_request(token: str) -> urllib.request.Request:
    """Build the read-only profile request without putting the token in its URL."""
    query = urllib.parse.urlencode({"fields": "user_id,username"})
    return urllib.request.Request(
        f"{GRAPH_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )


def _redact(value: str, token: str) -> str:
    """Keep an upstream error useful without ever echoing the credential."""
    clean = " ".join(value.split())
    return clean.replace(token, "[redacted]") if token else clean


def _api_error(status: int, body: bytes, token: str) -> MetaIntegrationError:
    """Turn Meta's JSON error envelope into a short, token-safe exception."""
    detail = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            kind = error.get("type")
            code = error.get("code")
            message = error.get("message")
            labels = []
            if isinstance(kind, str) and kind:
                labels.append(kind)
            if isinstance(code, (str, int)):
                labels.append(f"code {code}")
            if labels:
                detail += " (" + ", ".join(labels) + ")"
            if isinstance(message, str) and message:
                detail += ": " + _redact(message, token)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return MetaIntegrationError(f"Meta API rejected the credential (HTTP {status}){detail}")


def fetch_profile(token: str, opener=None) -> dict:
    """Return the authenticated Instagram profile from Meta.

    ``opener`` is resolved at call time so unit tests can inject an offline
    response.  The live path uses ``urllib.request.urlopen`` and no third-party
    dependency.
    """
    token = token.strip()
    if not token:
        raise MetaIntegrationError(
            "INSTAGRAM_ACCESS_TOKEN is missing; replace the repository secret's "
            "placeholder with an Instagram User access token"
        )

    opener = opener or urllib.request.urlopen
    try:
        with opener(profile_request(token), timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_ERROR_BYTES)
        raise _api_error(exc.code, body, token) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        # Avoid interpolating the network exception: some HTTP clients include
        # request details in those strings, and the token must never reach logs.
        raise MetaIntegrationError("could not reach the Meta API") from None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MetaIntegrationError("Meta API returned a non-JSON response") from None
    if not isinstance(payload, dict):
        raise MetaIntegrationError("Meta API returned an unexpected response shape")
    return payload


def check_integration(
    token: str,
    expected_username: str = EXPECTED_USERNAME,
    opener=None,
) -> str:
    """Validate the token's profile and return its normalized username."""
    profile = fetch_profile(token, opener=opener)
    username = profile.get("username")
    user_id = profile.get("user_id") or profile.get("id")
    if not isinstance(username, str) or not username.strip() or not user_id:
        raise MetaIntegrationError(
            "Meta API response is missing the Instagram user_id or username"
        )

    actual = username.strip().lstrip("@").lower()
    expected = expected_username.strip().lstrip("@").lower()
    if not expected:
        raise MetaIntegrationError("the expected Instagram username is empty")
    if actual != expected:
        raise MetaIntegrationError(
            f"token belongs to @{actual}, not the expected @{expected} account"
        )
    return actual


def main() -> int:
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    expected = os.environ.get(
        "META_EXPECTED_INSTAGRAM_USERNAME", EXPECTED_USERNAME
    )
    try:
        username = check_integration(token, expected)
    except MetaIntegrationError as exc:
        print(f"Meta integration check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Meta integration check passed for @{username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
