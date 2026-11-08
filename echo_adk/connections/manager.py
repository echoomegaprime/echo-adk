"""Owner-bound OAuth consent and per-bot connector execution.

The outer host must authenticate the owner before calling these methods. Worker
integration must separately enforce its exact connection/operation allowlist,
Lease and run budget; an OAuth token does not grant an agent authority by itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.deadline import limit_io
from .catalog import ENV_NAME, Provider, builtin_providers, identifier, redirect_uri
from .errors import ConnectionError
from .store import ConnectionStore, Vault
from .transport import HttpTransport, parse_json, public_result


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _approved_binding(public: dict) -> str:
    fields = {key: public[key] for key in ("id", "bot_id", "provider_id", "operation_ids", "scopes")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _credential(value: Any, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ConnectionError("invalid_provider_response", "The provider returned an invalid credential field.", 502, outcome_unknown=True)
    return value


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    secret_reader: Callable[[], str | None] = field(repr=False)

    def secret(self, required: bool = True) -> str | None:
        try:
            value = self.secret_reader()
            return _credential(value, required=required)
        except Exception:
            raise ConnectionError("provider_not_configured", "The provider's host credential reference is unavailable.", 503) from None


class ConnectionManager:
    def __init__(
        self,
        *,
        store: ConnectionStore | None,
        clients: dict[str, OAuthClient],
        redirect: str | None,
        require_bot: Callable[[str], Any],
        owner_id: str = "prime-owner",
        providers: tuple[Provider, ...] | None = None,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.time,
        unavailable_reason: str | None = None,
        state_ttl_s: int = 600,
        vault_path: Path | None = None,
    ) -> None:
        if not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 256 or any(ord(c) < 32 for c in owner_id):
            raise ConnectionError("invalid_configuration", "A fixed host owner identity is required.")
        if not callable(require_bot) or not isinstance(state_ttl_s, int) or isinstance(state_ttl_s, bool) or not 30 <= state_ttl_s <= 600:
            raise ConnectionError("invalid_configuration", "A bot authorizer and bounded consent lifetime are required.")
        self.owner_id, self._require_bot = owner_id, require_bot
        self.store, self.clients, self.redirect = store, dict(clients), redirect_uri(redirect) if redirect else None
        self.transport, self.clock, self.state_ttl_s = transport or HttpTransport(), clock, state_ttl_s
        self.unavailable_reason = unavailable_reason
        self._vault_path = vault_path
        configured = providers if providers is not None else builtin_providers()
        if len({p.id for p in configured}) != len(configured):
            raise ConnectionError("invalid_configuration", "Provider IDs must be unique.")
        for provider in configured:
            provider.validate()
        self.providers = {p.id: p for p in configured}
        if set(self.clients) - set(self.providers):
            raise ConnectionError("invalid_configuration", "Only declared providers can receive client credentials.")

    def _bot(self, bot_id: str) -> str:
        identifier(bot_id, "bot")
        # Trusted callback must raise for absent or inaccessible bots. The
        # caller cannot replace this callback or the configured owner identity.
        self._require_bot(bot_id)
        return bot_id

    def _provider(self, provider_id: str) -> Provider:
        identifier(provider_id, "provider")
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ConnectionError("provider_not_found", "This provider is not in the host connector catalog.", 404)
        return provider

    def _ready(self, provider: Provider) -> OAuthClient:
        if self.store is None or self.redirect is None:
            raise ConnectionError(
                "connections_unavailable",
                self.unavailable_reason or "The host has not configured an encrypted connection vault and callback.",
                503,
            )
        self.store.vault.check_key()
        client = self.clients.get(provider.id)
        if client is None:
            raise ConnectionError("provider_not_configured", "The host has not enabled this OAuth provider.", 503)
        client.secret(required=provider.token_auth != "none")
        return client

    def catalog(self) -> dict:
        providers = []
        for provider in self.providers.values():
            unavailable = None
            try:
                self._ready(provider)
            except ConnectionError as exc:
                unavailable = exc.message
            providers.append(
                {
                    "id": provider.id,
                    "name": provider.name,
                    "description": provider.description,
                    "configured": unavailable is None,
                    "unavailable_reason": unavailable,
                    "operations": [operation.public() for operation in provider.operations],
                    "documentation_url": provider.documentation_url,
                    "revocation_supported": provider.revoke_url is not None,
                    "revocation_effect": provider.revocation_effect,
                }
            )
        return {"configured": any(p["configured"] for p in providers), "providers": providers}

    def list(self, bot_id: str) -> list[dict]:
        self._bot(bot_id)
        return self.store.list(self.owner_id, bot_id) if self.store is not None else []

    def has_resources(self, bot_id: str) -> bool:
        """Metadata-only rollback guard, including incomplete owner consent."""
        self._bot(bot_id)
        if self.store is not None:
            return bool(self.store.list(self.owner_id, bot_id))
        # An unavailable vault is not proof that a bot has no owner resources.
        return self._vault_path is not None and self._vault_path.exists()

    def get(self, bot_id: str, connection_id: str) -> dict:
        self._bot(bot_id)
        identifier(connection_id, "connection")
        if self.store is None:
            raise ConnectionError("connection_not_found", "Connection not found for this bot.", 404)
        return self.store.get(self.owner_id, bot_id, connection_id)[0]

    def _binding(self, public: dict, binding: str) -> tuple[Provider, OAuthClient]:
        provider = self._provider(public["provider_id"])
        client = self._ready(provider)
        if binding != provider.fingerprint(client.client_id, self.redirect):
            raise ConnectionError(
                "connection_configuration_changed", "Provider configuration changed; disconnect and obtain fresh owner consent.", 409
            )
        return provider, client

    def start(self, bot_id: str, provider_id: str, operation_ids: list[str]) -> dict:
        self._bot(bot_id)
        provider = self._provider(provider_id)
        client = self._ready(provider)
        if (
            not isinstance(operation_ids, list)
            or not 1 <= len(operation_ids) <= 32
            or any(not isinstance(x, str) for x in operation_ids)
            or len(set(operation_ids)) != len(operation_ids)
        ):
            raise ConnectionError("invalid", "Choose a nonempty, unique list of provider operation IDs.")
        operations = [provider.operation(identifier(op, "operation")) for op in operation_ids]
        scopes = sorted({scope for operation in operations for scope in operation.scopes} | set(provider.auxiliary_scopes))
        now = self.clock()
        state, verifier, connection = secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_hex(16)
        expires = now + self.state_ttl_s
        public = {
            "id": connection,
            "bot_id": bot_id,
            "provider_id": provider.id,
            "provider_name": provider.name,
            "status": "pending",
            "operation_ids": sorted(operation_ids),
            "scopes": scopes,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "authorization_expires_at": _iso(expires),
        }
        self.store.create(
            self.owner_id,
            public,
            provider.fingerprint(client.client_id, self.redirect),
            hashlib.sha256(state.encode()).hexdigest(),
            expires,
            {"verifier": verifier, "redirect_uri": self.redirect, "approved_binding": _approved_binding(public)},
            now,
        )
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = {
            **provider.authorization_extra,
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": self.redirect,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return {
            "connection": {**public, "busy": False},
            "authorization_url": provider.authorization_url + "?" + urllib.parse.urlencode(query),
            "expires_at": _iso(expires),
        }

    @staticmethod
    def _state_hash(state: str) -> str:
        if not isinstance(state, str) or not 32 <= len(state) <= 128 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in state):
            raise ConnectionError("invalid_oauth_state", "OAuth consent state is invalid, expired, or already used.")
        return hashlib.sha256(state.encode()).hexdigest()

    def complete(self, bot_id: str, connection_id: str, state: str, code: str, redirect_uri: str | None = None) -> dict:
        self._bot(bot_id)
        identifier(connection_id, "connection")
        if redirect_uri is not None and redirect_uri != self.redirect:
            raise ConnectionError("invalid_oauth_state", "OAuth callback does not match the configured redirect.")
        return self._complete(state, code, expected=(bot_id, connection_id))

    def complete_callback(self, state: str, code: str) -> dict:
        """Resolve the original bot/connection solely from owner-bound server state."""
        return self._complete(state, code)

    def _complete(self, state: str, code: str, expected: tuple[str, str] | None = None) -> dict:
        state_hash = self._state_hash(state)
        if not isinstance(code, str) or not 1 <= len(code) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in code):
            raise ConnectionError("invalid", "A valid OAuth authorization code is required.")
        if self.store is None:
            raise ConnectionError("connections_unavailable", "The encrypted connection vault is unavailable.", 503)
        public, binding = self.store.lookup_state(self.owner_id, state_hash)
        bot, connection = public["bot_id"], public["id"]
        self._bot(bot)
        if expected is not None and expected != (bot, connection):
            raise ConnectionError("invalid_oauth_state", "OAuth state is bound to another bot or connection.")
        provider, client = self._binding(public, binding)
        claim, verifier = self.store.claim_state(self.owner_id, bot, connection, state_hash, self.clock())
        response = None
        try:
            if verifier.get("approved_binding") != _approved_binding(public):
                raise ConnectionError("connection_integrity", "Connection permissions no longer match the encrypted consent record.", 409)
            response = self._token_request(
                provider,
                client,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier["verifier"],
                    "redirect_uri": verifier["redirect_uri"],
                },
            )
            tokens = self._tokens(provider, public, response)
            public = self._connected(public, tokens)
            if not self.store.finish(self.owner_id, bot, connection, claim, public, tokens):
                self._revoke(provider, client, tokens)
                raise ConnectionError("connection_changed", "The connection was disconnected while consent completed.", 409)
            return self.get(bot, connection)
        except ConnectionError as exc:
            failed = {**public, "status": "error", "updated_at": _iso(self.clock()), "last_error": exc.as_dict()}
            quarantine = self._quarantine(public, response)
            if quarantine:
                failed["cleanup_required"] = True
            self.store.finish(self.owner_id, bot, connection, claim, failed, quarantine)
            raise

    def _token_request(self, provider: Provider, client: OAuthClient, form: dict) -> dict:
        secret = client.secret(required=provider.token_auth != "none")
        form, headers = {**form, "client_id": client.client_id}, {}
        if provider.token_auth == "client_secret_post":
            form["client_secret"] = secret
        elif provider.token_auth == "client_secret_basic":
            pair = urllib.parse.quote_plus(client.client_id) + ":" + urllib.parse.quote_plus(secret)
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        result = self.transport.request("POST", provider.token_url, headers=headers, form=form)
        if not isinstance(result, dict):
            raise ConnectionError(
                "invalid_provider_response", "The provider did not issue a usable token response.", 502, outcome_unknown=True
            )
        return result

    def _tokens(self, provider: Provider, public: dict, response: dict, previous: dict | None = None) -> dict:
        if response.get("error"):
            raise ConnectionError(
                "invalid_provider_response", "The provider did not issue a usable token response.", 502, outcome_unknown=True
            )
        if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
            raise ConnectionError("invalid_provider_response", "Only OAuth Bearer tokens are supported.", 502, outcome_unknown=True)
        token = _credential(response.get("access_token"))
        refresh = _credential(response.get("refresh_token"), required=False)
        expected = set(public["scopes"])
        supplied = response.get("scope")
        if supplied is None:
            granted = expected  # RFC 6749: omitted scope is identical to requested scope.
        elif isinstance(supplied, str) and len(supplied) <= 8192:
            granted = set((supplied.replace(",", " ") if provider.id == "github" else supplied).split())
        else:
            raise ConnectionError("invalid_provider_response", "The provider returned an invalid scope receipt.", 502, outcome_unknown=True)
        required = {scope for op_id in public["operation_ids"] for scope in provider.operation(op_id).scopes}
        if not required.issubset(granted) or not granted.issubset(expected):
            raise ConnectionError(
                "scope_mismatch",
                "The provider grant does not match this bot's approved scopes; obtain fresh consent.",
                403,
                outcome_unknown=True,
            )
        expires_in = response.get("expires_in")
        if expires_in is not None and (not isinstance(expires_in, int) or isinstance(expires_in, bool) or not 1 <= expires_in <= 31536000):
            raise ConnectionError(
                "invalid_provider_response", "The provider returned an invalid token lifetime.", 502, outcome_unknown=True
            )
        if previous and previous.get("expires_at") is not None and expires_in is None:
            raise ConnectionError(
                "invalid_provider_response", "A refreshed expiring token must include its lifetime.", 502, outcome_unknown=True
            )
        return {
            "access_token": token,
            "refresh_token": refresh or (previous or {}).get("refresh_token"),
            "approved_binding": _approved_binding(public),
            "expires_at": self.clock() + expires_in if expires_in is not None else None,
        }

    def _connected(self, public: dict, tokens: dict) -> dict:
        public = {**public, "status": "connected", "updated_at": _iso(self.clock())}
        public.pop("last_error", None)
        public.pop("authorization_expires_at", None)
        public.pop("cleanup_required", None)
        if tokens.get("expires_at") is None:
            public.pop("expires_at", None)
        else:
            public["expires_at"] = _iso(tokens["expires_at"])
        public["refresh_supported"] = bool(tokens.get("refresh_token"))
        return public

    def _claimed(self, bot_id: str, connection_id: str) -> tuple[Provider, OAuthClient, str, dict, dict]:
        self._bot(bot_id)
        identifier(connection_id, "connection")
        if self.store is None:
            raise ConnectionError("connections_unavailable", "The encrypted connection vault is unavailable.", 503)
        public, binding = self.store.get(self.owner_id, bot_id, connection_id)
        provider, client = self._binding(public, binding)
        claim, public, tokens = self.store.claim(self.owner_id, bot_id, connection_id)
        if tokens.get("revocation_only") is True or tokens.get("approved_binding") != _approved_binding(public):
            public["status"] = "reauth_required"
            self.store.finish(self.owner_id, bot_id, connection_id, claim, public)
            raise ConnectionError("connection_integrity", "Connection permissions no longer match the encrypted consent record.", 409)
        return provider, client, claim, public, tokens

    def _refresh_response(self, provider: Provider, client: OAuthClient, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            raise ConnectionError("refresh_unavailable", "This provider did not issue a refresh token; reconnect when access expires.", 409)
        return self._token_request(provider, client, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})

    @staticmethod
    def _quarantine(public: dict, response: dict | None) -> dict | None:
        """Preserve rejected issuance only for an explicit owner's disconnect.

        Automatic broad provider revocation could break another healthy bot's
        grant. Quarantined credentials can never authorize an API or refresh.
        """
        if not isinstance(response, dict):
            return None
        held = {}
        for key in ("access_token", "refresh_token"):
            try:
                held[key] = _credential(response.get(key), required=False)
            except ConnectionError:
                held[key] = None
        if not any(held.values()):
            return None
        return {**held, "approved_binding": _approved_binding(public), "revocation_only": True, "expires_at": None}

    def refresh(self, bot_id: str, connection_id: str) -> dict:
        provider, client, claim, public, tokens = self._claimed(bot_id, connection_id)
        response = None
        try:
            response = self._refresh_response(provider, client, tokens)
            renewed = self._tokens(provider, public, response, tokens)
            public = self._connected(public, renewed)
            if not self.store.finish(self.owner_id, bot_id, connection_id, claim, public, renewed):
                self._revoke(provider, client, renewed)
                raise ConnectionError("connection_changed", "The connection was disconnected while refresh completed.", 409)
            return self.get(bot_id, connection_id)
        except ConnectionError as exc:
            if exc.code not in ("refresh_unavailable", "connection_changed"):
                public["status"] = "reauth_required"
            public.update(updated_at=_iso(self.clock()), last_error=exc.as_dict())
            quarantine = self._quarantine(public, response)
            if quarantine:
                public["cleanup_required"] = True
            self.store.finish(self.owner_id, bot_id, connection_id, claim, public, quarantine)
            raise

    def invoke(self, bot_id: str, connection_id: str, operation_id: str, params: dict | None = None) -> dict:
        # Validate before the durable claim, so bad model arguments do not leave
        # a connection busy. No operation can supply its own URL or header.
        public = self.get(bot_id, connection_id)
        provider = self._provider(public["provider_id"])
        operation = provider.operation(identifier(operation_id, "operation"))
        if operation_id not in public["operation_ids"]:
            raise ConnectionError("operation_not_allowed", "This operation is not bound to this bot's connection.", 403)
        url, body = operation.request({} if params is None else params)
        provider, client, claim, public, tokens = self._claimed(bot_id, connection_id)
        refreshing = False
        response = None
        try:
            if tokens.get("expires_at") is not None and tokens["expires_at"] <= self.clock() + 15:
                refreshing = True
                response = self._refresh_response(provider, client, tokens)
                tokens = self._tokens(provider, public, response, tokens)
                public = self._connected(public, tokens)
                refreshing = False
            secret = client.secret(required=provider.token_auth != "none")
            result = self.transport.request(
                operation.method, url, headers={**provider.api_headers, "Authorization": "Bearer " + tokens["access_token"]}, body=body
            )
            if isinstance(result, dict) and (result.get("ok") is False or result.get("error")):
                raise ConnectionError("provider_rejected", "The provider reported an unsuccessful operation.", 502)
            if provider.id == "google_drive" and operation.id == "files" and isinstance(result, dict):
                result = dict(result)
                cursor = result.pop("nextPageToken", None)
                if isinstance(cursor, str) and len(cursor) <= 2048:
                    # A page cursor is not an account credential. Use a name
                    # which survives the host's generic *token redaction.
                    result["next_cursor"] = cursor
            cleaned = public_result(result, tuple(x for x in (tokens["access_token"], tokens.get("refresh_token"), secret) if x))
            public.update(last_used_at=_iso(self.clock()), updated_at=_iso(self.clock()))
            if not self.store.finish(self.owner_id, bot_id, connection_id, claim, public, tokens):
                raise ConnectionError(
                    "connection_changed",
                    "The connection was disconnected while the operation completed.",
                    409,
                    outcome_unknown=not operation.read_only,
                )
            return {"ok": True, "connection_id": connection_id, "operation_id": operation_id, "data": cleaned, "untrusted": True}
        except ConnectionError as exc:
            if refreshing or exc.provider_status == 401:
                public["status"] = "reauth_required"
            if refreshing:
                quarantine = self._quarantine(public, response)
                if quarantine:
                    tokens, public["cleanup_required"] = quarantine, True
            public.update(updated_at=_iso(self.clock()), last_error=exc.as_dict())
            self.store.finish(self.owner_id, bot_id, connection_id, claim, public, tokens)
            raise

    def _revoke(self, provider: Provider, client: OAuthClient, tokens: dict) -> dict:
        receipt = {"provider_revoked": False, "revocation_status": "unsupported"}
        if provider.revoke_url is None:
            return receipt
        token = tokens.get("refresh_token") or tokens.get("access_token")
        if not token:
            return {"provider_revoked": False, "revocation_status": "not_needed"}
        try:
            secret = client.secret(required=provider.token_auth != "none" or provider.revoke_style == "github")
            if provider.revoke_style == "github":
                if not tokens.get("access_token"):
                    return {"provider_revoked": False, "revocation_status": "unavailable"}
                pair = client.client_id + ":" + secret
                url = provider.revoke_url.replace("{client_id}", urllib.parse.quote(client.client_id, safe=""))
                # GitHub's delete-token endpoint takes an access token. A
                # refresh token is never sent to an undocumented API endpoint.
                result = self.transport.request(
                    "DELETE",
                    url,
                    headers={**provider.api_headers, "Authorization": "Basic " + base64.b64encode(pair.encode()).decode()},
                    body={"access_token": tokens["access_token"]},
                    allow_empty=True,
                )
            else:
                form = {
                    "token": token,
                    "token_type_hint": "refresh_token" if tokens.get("refresh_token") else "access_token",
                    "client_id": client.client_id,
                }
                headers = {}
                if provider.token_auth == "client_secret_post":
                    form["client_secret"] = secret
                elif provider.token_auth == "client_secret_basic":
                    pair = urllib.parse.quote_plus(client.client_id) + ":" + urllib.parse.quote_plus(secret)
                    headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
                result = self.transport.request("POST", provider.revoke_url, headers=headers, form=form, allow_empty=True)
            if isinstance(result, dict) and (result.get("ok") is False or result.get("error")):
                return {"provider_revoked": False, "revocation_status": "failed"}
            return {"provider_revoked": True, "revocation_status": "confirmed"}
        except ConnectionError as exc:
            if exc.code == "provider_not_started":
                return {"provider_revoked": False, "revocation_status": "pending"}
            return {"provider_revoked": False, "revocation_status": "unknown" if exc.outcome_unknown else "failed"}

    def disconnect(self, bot_id: str, connection_id: str) -> dict:
        self._bot(bot_id)
        public = self.get(bot_id, connection_id)
        if public["status"] == "disconnected":
            return {
                "connection_id": connection_id,
                "status": "disconnected",
                **public.get("revocation", {"provider_revoked": False, "revocation_status": "unknown"}),
            }
        # Erase local credentials first. Provider failure must never restore
        # access, and a late callback/refresh cannot pass the invalidated claim.
        public, tokens, uncertain = self.store.detach(self.owner_id, bot_id, connection_id, _iso(self.clock()))
        provider = self.providers.get(public["provider_id"])
        client = self.clients.get(public["provider_id"])
        receipt = {"provider_revoked": False, "revocation_status": "not_needed"}
        if tokens:
            receipt = (
                self._revoke(provider, client, tokens)
                if provider and client
                else {"provider_revoked": False, "revocation_status": "unavailable"}
            )
        if receipt["revocation_status"] == "pending" and tokens:
            retained = self.store.retain_unattempted(self.owner_id, bot_id, connection_id, public, tokens, uncertain)
            return {
                "connection_id": connection_id,
                "status": "reauth_required" if retained else "disconnected",
                "provider_revoked": False,
                "revocation_status": "pending" if retained else "unknown",
            }
        if uncertain:
            receipt = {"provider_revoked": False, "revocation_status": "unknown"}
        self.store.set_revocation(self.owner_id, bot_id, connection_id, receipt)
        return {"connection_id": connection_id, "status": "disconnected", **receipt}

    def delete_bot(self, bot_id: str, *, revocation_budget_s: float = 20.0) -> dict:
        self._bot(bot_id)
        if isinstance(revocation_budget_s, bool) or not isinstance(revocation_budget_s, (int, float)) or not 0 < revocation_budget_s <= 20:
            raise ConnectionError("invalid", "The aggregate revocation allowance must be positive and at most 20 seconds.")
        if self.store is None and self._vault_path is not None and self._vault_path.exists():
            raise ConnectionError(
                "credential_store_unavailable",
                "Restore the host vault configuration before confirming this bot's connection deletion.",
                503,
            )
        receipts, pending = [], []
        deadline = time.monotonic() + revocation_budget_s
        if self.store is not None:
            held = self.store.hold_for_deletion(self.owner_id, bot_id, _iso(self.clock()))
            for public, has_secret in held:
                remaining = deadline - time.monotonic()
                if public["status"] != "disconnected" and has_secret and remaining <= 0:
                    receipt = {
                        "connection_id": public["id"],
                        "status": "reauth_required",
                        "provider_revoked": False,
                        "revocation_status": "pending",
                    }
                else:
                    with limit_io(max(0.0, remaining)):
                        receipt = self.disconnect(bot_id, public["id"])
                receipts.append(receipt)
                if receipt["revocation_status"] == "pending":
                    pending.append(public["id"])
            if not pending:
                self.store.delete_bot(self.owner_id, bot_id)
        return {"bot_id": bot_id, "deleted": not pending, "connections": receipts, "pending_connection_ids": pending}

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


def _env_reference(value: Any) -> str:
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise ConnectionError("invalid_configuration", "Credential references must name host environment variables.")
    return value


def from_env(state_dir: str | Path, require_bot: Callable[[str], Any], owner_id: str = "prime-owner") -> ConnectionManager:
    """Construct the real host adapter, or an honest disabled catalog.

    Configuration contains environment-variable *names*, never literal secrets.
    A missing extra/key/config does not break unrelated stdlib-only ADK service.
    Invalid configured inputs disable connectors; they never select a fallback.
    """
    clients, store, redirect = {}, None, None
    unavailable = "The host has not configured OAuth providers, a callback, and an encrypted credential-vault key."
    try:
        config_path = os.environ.get("ECHO_ADK_CONNECTIONS_CONFIG")
        if config_path:
            with Path(config_path).open("rb") as source:
                raw = source.read(32769)
            if len(raw) > 32768:
                raise ConnectionError("invalid_configuration", "Connector configuration exceeds its size bound.")
            config = parse_json(raw)
            if not isinstance(config, dict) or set(config) != {"providers"} or not isinstance(config["providers"], dict):
                raise ConnectionError("invalid_configuration", "Connector configuration must contain only the provider reference map.")
            templates = {p.id: p for p in builtin_providers()}
            if set(config["providers"]) - set(templates):
                raise ConnectionError("invalid_configuration", "Only checked provider templates can be enabled by configuration.")
            for name, spec in config["providers"].items():
                if not isinstance(spec, dict) or set(spec) != {"client_id_env", "client_secret_env"}:
                    raise ConnectionError(
                        "invalid_configuration", "Provider configuration requires client ID and secret environment references."
                    )
                id_env, secret_env = _env_reference(spec["client_id_env"]), _env_reference(spec["client_secret_env"])
                client_id = os.environ.get(id_env)
                if not isinstance(client_id, str) or not 1 <= len(client_id) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in client_id):
                    continue  # A single unconfigured provider must not claim readiness.
                clients[name] = OAuthClient(client_id, lambda name=secret_env: os.environ.get(name))
            redirect = redirect_uri(os.environ.get("ECHO_ADK_CONNECTIONS_REDIRECT_URI", ""))
            key_env = _env_reference(os.environ.get("ECHO_ADK_CONNECTIONS_KEY_ENV", "ECHO_ADK_CONNECTIONS_KEY"))

            def key_provider() -> bytes:
                try:
                    value = os.environ.get(key_env, "")
                    return base64.b64decode(value, validate=True)
                except (ValueError, TypeError):
                    raise ConnectionError("credential_store_unavailable", "The host vault-key reference is invalid.", 503) from None

            vault = Vault(key_provider)
            store = ConnectionStore(Path(state_dir) / "connections" / "vault.sqlite3", vault)
            unavailable = None
    except ConnectionError as exc:
        unavailable = exc.message
    except Exception:
        unavailable = "The host connector configuration or encrypted vault is unavailable."
    return ConnectionManager(
        store=store,
        clients=clients,
        redirect=redirect,
        require_bot=require_bot,
        owner_id=owner_id,
        unavailable_reason=unavailable,
        vault_path=Path(state_dir) / "connections" / "vault.sqlite3",
    )
