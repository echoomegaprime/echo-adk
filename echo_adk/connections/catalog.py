"""Host-owned OAuth endpoints and a closed, inspectable operation catalogue.

Neither OAuth endpoints nor HTTP headers are accepted from an owner API or a
model. A host can construct additional Provider records in trusted code; the
environment factory enables only these checked-in provider templates.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .errors import ConnectionError

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def identifier(value: Any, label: str = "identifier") -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ConnectionError("invalid", f"Invalid {label}.")
    return value


def endpoint(value: str, *, allow_loopback_http: bool = False, query: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ConnectionError("invalid_configuration", "Invalid configured URL.")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise ConnectionError("invalid_configuration", "Invalid configured URL.") from None
    if not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment or (parts.query and not query):
        raise ConnectionError("invalid_configuration", "Configured URLs cannot contain credentials, fragments or unexpected queries.")
    if "\\" in value or parts.hostname != parts.hostname.encode("idna").decode("ascii") or port == 0:
        raise ConnectionError("invalid_configuration", "Configured URLs must have an unambiguous host.")
    loopback = parts.hostname.lower() == "localhost"
    try:
        address = ipaddress.ip_address(parts.hostname)
        loopback = address.is_loopback
        if not address.is_global and not (allow_loopback_http and loopback):
            raise ConnectionError("invalid_configuration", "Provider endpoints must use a public host.")
    except ValueError:
        if parts.hostname.endswith(".localhost") or parts.hostname.lower() in ("localhost", "metadata.google.internal"):
            if not (allow_loopback_http and loopback):
                raise ConnectionError("invalid_configuration", "Provider endpoints must use a public host.")
    if parts.scheme != "https" and not (parts.scheme == "http" and allow_loopback_http and loopback):
        raise ConnectionError("invalid_configuration", "Configured provider endpoints require HTTPS.")
    return value


def redirect_uri(value: str) -> str:
    # A host may run the owner UI on a loopback HTTP origin. A public callback
    # always requires HTTPS and must be registered verbatim with the provider.
    return endpoint(value, allow_loopback_http=True)


def object_schema(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def validate_params(value: Any, schema: dict, depth: int = 0) -> None:
    """Validate the deliberately small, closed subset used by operation templates."""
    if depth > 8:
        raise ConnectionError("invalid", "Operation parameters are too deeply nested.")
    kind = schema.get("type")
    valid = False
    if kind == "object":
        valid = isinstance(value, dict) and len(value) <= 32
        if valid:
            properties = schema.get("properties", {})
            valid = all(isinstance(k, str) and k in properties for k in value) and all(k in value for k in schema.get("required", ()))
            if valid:
                for key, item in value.items():
                    validate_params(item, properties[key], depth + 1)
    elif kind == "string":
        valid = isinstance(value, str) and schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 2048)
        if valid and "pattern" in schema:
            valid = re.fullmatch(schema["pattern"], value) is not None
        if valid:
            valid = not any(ord(c) < 32 or ord(c) == 127 for c in value)
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool) and schema.get("minimum", 0) <= value <= schema.get("maximum", 10000)
    elif kind == "boolean":
        valid = isinstance(value, bool)
    if "enum" in schema:
        valid = valid and value in schema["enum"]
    if not valid:
        raise ConnectionError("invalid", "Operation parameters do not match its declared schema.")


@dataclass(frozen=True)
class Operation:
    id: str
    name: str
    description: str
    url: str
    scopes: tuple[str, ...]
    input_schema: dict = field(default_factory=object_schema)
    method: str = "GET"
    read_only: bool = True
    # Parameter name -> (location: path/query/body, provider parameter name).
    parameters: dict[str, tuple[str, str]] = field(default_factory=dict)
    fixed_query: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "scopes": list(self.scopes),
            "method": self.method,
            "read_only": self.read_only,
            "input_schema": copy.deepcopy(self.input_schema),
        }

    def request(self, params: dict) -> tuple[str, dict | None]:
        validate_params(params, self.input_schema)
        url, query, body = self.url, dict(self.fixed_query), {}
        for name, value in params.items():
            location, target = self.parameters[name]
            if location == "path":
                url = url.replace("{" + target + "}", urllib.parse.quote(str(value), safe=""))
            elif location == "query":
                query[target] = str(value).lower() if isinstance(value, bool) else str(value)
            else:
                body[target] = value
        if "{" in url or "}" in url:
            raise ConnectionError("invalid", "Missing operation path parameter.")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url, body or None


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    description: str
    authorization_url: str
    token_url: str
    documentation_url: str
    operations: tuple[Operation, ...]
    revoke_url: str | None = None
    revoke_style: str = "rfc7009"
    revocation_effect: str = "Revokes this token at the provider when its endpoint confirms success."
    auxiliary_scopes: tuple[str, ...] = ()
    authorization_extra: dict[str, str] = field(default_factory=dict)
    token_auth: str = "client_secret_post"
    api_headers: dict[str, str] = field(default_factory=dict)
    scope_separator: str = " "
    allow_loopback_http: bool = False  # Trusted test hosts only; never set by environment/API input.

    def validate(self) -> None:
        identifier(self.id, "provider")
        endpoint(self.authorization_url, allow_loopback_http=self.allow_loopback_http)
        endpoint(self.token_url, allow_loopback_http=self.allow_loopback_http)
        if self.revoke_url:
            endpoint(self.revoke_url.replace("{client_id}", "client"), allow_loopback_http=self.allow_loopback_http)
        if self.token_auth not in ("client_secret_post", "client_secret_basic", "none") or self.revoke_style not in ("rfc7009", "github"):
            raise ConnectionError("invalid_configuration", "Unsupported provider authentication or revocation mode.")
        if not self.operations or len(self.operations) > 32 or len({o.id for o in self.operations}) != len(self.operations):
            raise ConnectionError("invalid_configuration", "Provider operations must be unique and bounded.")
        reserved = {"response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method"}
        if reserved & self.authorization_extra.keys():
            raise ConnectionError("invalid_configuration", "Provider cannot override OAuth security parameters.")
        for key, value in self.api_headers.items():
            if key.lower() not in ("accept", "x-github-api-version") or not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ConnectionError("invalid_configuration", "Unsupported configured API header.")
        for op in self.operations:
            identifier(op.id, "operation")
            endpoint(op.url, allow_loopback_http=self.allow_loopback_http)
            if op.method not in ("GET", "POST", "PUT", "PATCH", "DELETE") or (op.read_only and op.method != "GET"):
                raise ConnectionError("invalid_configuration", "Invalid operation HTTP method.")
            if op.input_schema.get("type") != "object" or op.input_schema.get("additionalProperties") is not False:
                raise ConnectionError("invalid_configuration", "Operation parameters must have a closed object schema.")
            if set(op.parameters) != set(op.input_schema.get("properties", {})):
                raise ConnectionError("invalid_configuration", "Operation parameter bindings must match its schema.")
            for location, _ in op.parameters.values():
                if location not in ("path", "query", "body") or (location == "body" and op.method == "GET"):
                    raise ConnectionError("invalid_configuration", "Invalid operation parameter binding.")
            for scope in (*op.scopes, *self.auxiliary_scopes):
                if not isinstance(scope, str) or not 1 <= len(scope) <= 256 or any(ord(c) < 33 or ord(c) >= 127 for c in scope):
                    raise ConnectionError("invalid_configuration", "Invalid provider scope.")

    def operation(self, operation_id: str) -> Operation:
        for operation in self.operations:
            if operation.id == operation_id:
                return operation
        raise ConnectionError("operation_not_allowed", "This provider does not offer that operation.", 403)

    def fingerprint(self, client_id: str, redirect: str) -> str:
        # Endpoints, operations and consent semantics are pinned to the original
        # connection. A changed host configuration requires new owner consent.
        from dataclasses import asdict

        data = {"provider": asdict(self), "client_id": client_id, "redirect_uri": redirect}
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def builtin_providers() -> tuple[Provider, ...]:
    drive_scope = "https://www.googleapis.com/auth/drive.metadata.readonly"
    file_fields = "id,name,mimeType,modifiedTime,size,webViewLink"
    return (
        Provider(
            id="github",
            name="GitHub",
            description="Read your profile and public repository metadata with a bot-specific OAuth connection.",
            authorization_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            revoke_url="https://api.github.com/applications/{client_id}/token",
            revoke_style="github",
            revocation_effect=(
                "Revokes this access token. Other app grants or token pairs may remain; "
                "use GitHub's Authorized OAuth Apps settings to revoke the whole app."
            ),
            auxiliary_scopes=("offline_access",),
            documentation_url="https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",
            api_headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
            operations=(
                Operation(
                    "profile", "Read profile", "Read the authenticated GitHub profile.", "https://api.github.com/user", ("read:user",)
                ),
                Operation(
                    "repositories",
                    "List public repositories",
                    "List public repository metadata; private repository and write scopes are not requested.",
                    "https://api.github.com/user/repos",
                    ("read:user",),
                    object_schema(
                        {
                            "page": {"type": "integer", "minimum": 1, "maximum": 100},
                            "per_page": {"type": "integer", "minimum": 1, "maximum": 100},
                        }
                    ),
                    parameters={"page": ("query", "page"), "per_page": ("query", "per_page")},
                    fixed_query={"visibility": "public", "sort": "updated", "per_page": "30"},
                ),
            ),
        ),
        Provider(
            id="google_drive",
            name="Google Drive",
            description="Search and read Drive file metadata. This connector does not read file contents or modify files.",
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            revoke_url="https://oauth2.googleapis.com/revoke",
            revocation_effect=(
                "Google revocation removes the app/project grant and may disconnect other bots using that same account and OAuth app."
            ),
            authorization_extra={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"},
            documentation_url="https://developers.google.com/identity/protocols/oauth2/web-server",
            operations=(
                Operation(
                    "files",
                    "List file metadata",
                    "Search the connected account's Drive metadata with bounded pagination.",
                    "https://www.googleapis.com/drive/v3/files",
                    (drive_scope,),
                    object_schema(
                        {
                            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
                            "cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "q": {"type": "string", "minLength": 1, "maxLength": 1000},
                        }
                    ),
                    parameters={"page_size": ("query", "pageSize"), "cursor": ("query", "pageToken"), "q": ("query", "q")},
                    fixed_query={
                        "pageSize": "30",
                        "spaces": "drive",
                        "fields": f"nextPageToken,files({file_fields})",
                        "q": "trashed=false",
                    },
                ),
                Operation(
                    "file_metadata",
                    "Read file metadata",
                    "Read one file's metadata by its exact Drive file ID.",
                    "https://www.googleapis.com/drive/v3/files/{file_id}",
                    (drive_scope,),
                    object_schema(
                        {"file_id": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": "[A-Za-z0-9_-]+"}}, ("file_id",)
                    ),
                    parameters={"file_id": ("path", "file_id")},
                    fixed_query={"fields": file_fields},
                ),
            ),
        ),
    )
