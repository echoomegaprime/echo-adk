"""Bounded HTTPS transport and credential-free, untrusted result projection."""

from __future__ import annotations

import base64
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..core.deadline import io_timeout
from ..core.redact import redact_value
from .errors import ConnectionError

MAX_RESPONSE_BYTES = 512 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never send a token, code, verifier or secret to a redirect


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def parse_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant, parse_float=_finite_float)


class HttpTransport:
    def __init__(self, *, timeout_s: float = 20.0) -> None:
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not 0 < timeout_s <= 30:
            raise ConnectionError("invalid_configuration", "Connector HTTP timeout must be at most 30 seconds.")
        self.timeout_s = float(timeout_s)
        self._open = urllib.request.build_opener(_NoRedirect()).open

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        form: dict | None = None,
        body: dict | None = None,
        allow_empty: bool = False,
    ) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "echo-adk-connections/1", **headers}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        if data is not None and len(data) > 65536:
            raise ConnectionError("invalid", "Connector request exceeds its size bound.")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            timeout = io_timeout(self.timeout_s)
        except TimeoutError:
            raise ConnectionError(
                "provider_not_started", "The provider request was not started because its I/O allowance expired.", 503
            ) from None
        try:
            with self._open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if not 200 <= response.status < 300:
                    raise ConnectionError(
                        "provider_rejected", "The configured provider rejected the request.", 502, provider_status=response.status
                    )
        except urllib.error.HTTPError as exc:
            # Do not echo the response, URL, Location header or exception text:
            # those may contain credentials reflected by the provider.
            status = exc.code
            exc.close()
            raise ConnectionError(
                "provider_rejected", "The configured provider rejected the request.", 502, provider_status=status
            ) from None
        except ConnectionError:
            raise
        except Exception:
            raise ConnectionError(
                "provider_unavailable", "The provider request did not complete; its outcome may be unknown.", 502, outcome_unknown=True
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectionError("invalid_provider_response", "The provider response exceeds its size bound.", 502, outcome_unknown=True)
        if not raw and allow_empty:
            return {}
        try:
            return parse_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ConnectionError(
                "invalid_provider_response", "The provider returned an invalid JSON response.", 502, outcome_unknown=True
            ) from None


def public_result(value: Any, credentials: tuple[str, ...]) -> Any:
    """Strip issued credentials, common encoded reflections, and secret fields.

    Provider content remains untrusted data. It never changes a connection's
    owner, scopes, operation bindings or an agent's permission grants.
    """
    variants = set()
    for credential in credentials:
        if not credential:
            continue
        variants.add(credential)
        variants.add(base64.b64encode(credential.encode()).decode())
        variants.add(base64.urlsafe_b64encode(credential.encode()).decode().rstrip("="))
        quoted = credential
        for _ in range(3):
            quoted = urllib.parse.quote(quoted, safe="")
            variants.add(quoted)
        variants.add(urllib.parse.quote_plus(credential))
    ordered = tuple(sorted(variants, key=len, reverse=True))

    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 32:
            return "[REDACTED]"
        if isinstance(item, str):
            text = item
            # Nested JSON is a common transport wrapper; scrub after decoding so
            # unicode escapes cannot conceal an exact request credential.
            if text[:1] in ("{", "[", '"') and len(text) <= MAX_RESPONSE_BYTES:
                try:
                    decoded = parse_json(text.encode())
                    if decoded != item:
                        text = json.dumps(clean(decoded, depth + 1), ensure_ascii=False, separators=(",", ":"))
                except (ValueError, UnicodeError, RecursionError):
                    pass
            for secret in ordered:
                text = text.replace(secret, "[REDACTED]")
            return text
        if isinstance(item, dict):
            return {clean(str(key), depth + 1): clean(val, depth + 1) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val, depth + 1) for val in item]
        return item

    cleaned = clean(value)
    return redact_value(cleaned)[0]
