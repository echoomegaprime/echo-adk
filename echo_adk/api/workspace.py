"""Owner-controlled bot resources. Requests can never choose a host or credential.

The native API has one host owner. Prime and the SDK adapter authenticate that
owner before reaching this loopback surface. Browser profiles and OAuth grants
are always resolved from the verified bot id, never a caller-supplied path.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from typing import Any

from ..core.redact import redact_value
from ..core.types import is_id


class BotWorkspace:
    def __init__(self, service: Any) -> None:
        from ..bots.memory import BotMemoryRevisions
        from ..browser import BrowserConfig, BrowserManager
        from ..connections import from_env
        from ..connections.subscriptions import SubscriptionManager

        self.service = service
        self._resource_locks: dict[str, threading.RLock] = {}
        self._resource_guard = threading.RLock()
        self.browser = BrowserManager(service.state_dir, BrowserConfig.from_env())
        self.connections = from_env(service.state_dir, require_bot=self.require_bot, owner_id="prime-owner")
        self.memory = BotMemoryRevisions(service.state_dir)
        self.subscriptions = SubscriptionManager(service.state_dir, self.connections, self.require_bot, self.deliver_event)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle = threading.Condition()
        self._operation_local = threading.local()
        self._inflight = 0
        self._closing = False

    @contextmanager
    def admission(self):
        """Drain accepted operations before closing their persistent stores.

        The native HTTP handler and direct resource guards share this boundary.
        Re-entry on the admitted thread is permitted during shutdown so a
        multi-stage operation can persist its receipt and release its locks.
        """
        from .service import ApiError

        with self._lifecycle:
            depth = getattr(self._operation_local, "depth", 0)
            if depth == 0:
                if self._closing:
                    raise ApiError(503, "unavailable", "The bot workspace is shutting down")
                self._inflight += 1
            self._operation_local.depth = depth + 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._operation_local.depth -= 1
                if self._operation_local.depth == 0:
                    self._inflight -= 1
                    self._lifecycle.notify_all()

    def deliver_event(self, bot_id: str, envelope: dict[str, Any], still_current: Any) -> dict[str, Any]:
        from .service import ApiError

        with self.service.bots._lock:
            if not still_current():
                raise ApiError(409, "conflict", "The subscription changed before event admission")
            return self.service.bots.deliver_event(bot_id, envelope)

    def start(self) -> None:
        if self._thread is not None or self._closing:
            return

        def poll() -> None:
            from ..core.log import get_logger

            while not self._stop.wait(2):
                try:
                    self.subscriptions.poll_due(limit=4, cancel=self._stop)
                except Exception:
                    get_logger("subscriptions").warning("subscription_poll_failed")

        self._thread = threading.Thread(target=poll, name="echo-connection-events", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 60) -> None:
        deadline = time.monotonic() + timeout_s
        with self._lifecycle:
            self._closing = True
            self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise RuntimeError("A provider poll did not stop within the shutdown deadline; storage remains open")
        with self._lifecycle:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("An admitted API operation did not drain within the shutdown deadline; storage remains open")
                self._lifecycle.wait(remaining)

    def require_bot(self, bot_id: str) -> dict[str, Any]:
        from .service import ApiError

        if not is_id(bot_id):
            raise ApiError(400, "invalid", "Invalid bot id")
        return self.service.bots.get_bot(bot_id)

    @contextmanager
    def resource_guard(self, bot_ids: list[str]):
        with self.admission(), self._locked_resources(bot_ids):
            yield

    @contextmanager
    def _locked_resources(self, bot_ids: list[str]):
        from .service import ApiError

        if any(not is_id(bot_id) for bot_id in bot_ids):
            raise ApiError(400, "invalid", "Invalid bot resource id")
        with self._resource_guard:
            locks = [self._resource_locks.setdefault(bot_id, threading.RLock()) for bot_id in sorted(set(bot_ids))]
        acquired = []
        try:
            for lock in locks:
                if not lock.acquire(blocking=False):
                    raise ApiError(409, "resource_busy", "This bot has an active resource operation; inspect its state before retrying")
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def has_bot_resources(self, bot_id: str) -> bool:
        # Pure metadata. Unknown storage state makes rollback ineligible.
        try:
            return (
                self.browser.has_resources(bot_id)
                or self.connections.has_resources(bot_id)
                or (hasattr(self, "subscriptions") and self.subscriptions.has_resources(bot_id))
            )
        except Exception:
            return True

    @staticmethod
    def fields(body: Any, allowed: set[str], required: set[str] | None = None) -> dict[str, Any]:
        from .service import ApiError

        if not isinstance(body, dict) or set(body) - allowed or (required or set()) - set(body):
            raise ApiError(400, "invalid", "Invalid workspace request fields")
        return body

    @staticmethod
    def unpack(value: Any) -> dict[str, Any]:
        from .service import ApiError

        if not isinstance(value, str) or len(value.encode("utf-8")) > 262144:
            raise ApiError(400, "invalid", "Expected a JSON object of at most 256 KiB")
        try:
            body = json.loads(value)
        except (ValueError, RecursionError):
            raise ApiError(400, "invalid", "Invalid JSON object") from None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid", "Expected a JSON object")
        return body

    def close(self) -> None:
        self.stop()
        self.browser.shutdown()
        self.subscriptions.close()
        self.connections.close()
        self.memory.close()

    def delete(self, bot_id: str) -> dict[str, Any]:
        # Called only after BotManager has verified that active work is drained.
        from .service import ApiError

        try:
            self.subscriptions.disable_bot(bot_id)
            connections = self.connections.delete_bot(bot_id)
            if connections.get("deleted") is not True:
                raise ApiError(
                    409,
                    "cleanup_pending",
                    "The bot is paused; provider cleanup is pending. Retry deletion to finish the remaining connections.",
                    bot_id=bot_id,
                    deleted=False,
                    cleanup={"connections": connections, "subscriptions_disabled": True, "memory_deleted": False},
                )
            browser = self.browser.reset(bot_id)
            self.subscriptions.delete_bot(bot_id)
            self.memory.delete(bot_id)
        except ApiError:
            raise
        except Exception as error:
            if not hasattr(error, "code"):
                raise ApiError(500, "unavailable", "Bot cleanup could not be completed; the bot remains paused") from None
            raise ApiError(
                getattr(error, "status", 503),
                str(error.code),
                str(redact_value(str(error))[0]),
                bot_id=bot_id,
                deleted=False,
            ) from None
        return {"browser": browser, "connections": connections, "memory_deleted": True}

    def dispatch(self, operation: str, bot_id: str | None, body: Any, query: dict[str, str]) -> dict[str, Any]:
        with self.admission():
            return self._dispatch_admitted(operation, bot_id, body, query)

    def _dispatch_admitted(self, operation: str, bot_id: str | None, body: Any, query: dict[str, str]) -> dict[str, Any]:
        from .service import ApiError

        data = body if body is not None else {}
        try:
            if bot_id is not None and operation not in ("workspace_browser_close", "workspace_subscription_poll"):
                with self.resource_guard([bot_id]):
                    self.require_bot(bot_id)
                    return self._dispatch(operation, bot_id, data, query)
            if bot_id is not None:
                self.require_bot(bot_id)
            return self._dispatch(operation, bot_id, data, query)
        except ApiError:
            raise
        except Exception as error:
            code = getattr(error, "code", "invalid")
            status = getattr(error, "status", 400)
            if not isinstance(status, int) or not 400 <= status <= 599:
                status = 400
            # Provider libraries have deliberately generic public errors; an
            # unknown exception never reflects an OAuth response or traceback.
            if not hasattr(error, "code"):
                raise ApiError(500, "unavailable", "Bot workspace operation failed") from None
            unknown = getattr(error, "unknown_outcome", False) is True or getattr(error, "outcome_unknown", False) is True
            raise ApiError(status, str(code), str(redact_value(str(error))[0]), outcome_unknown=unknown) from None

    def _dispatch(self, operation: str, bot_id: str | None, data: Any, query: dict[str, str]) -> dict[str, Any]:
        from .service import ApiError

        if operation == "workspace_marketplace":
            return self.connections.catalog()
        if operation == "workspace_callback":
            d = self.fields(data, {"state", "code"}, {"state", "code"})
            return self.connections.complete_callback(d["state"], d["code"])
        if operation.startswith("workspace_subscription_"):
            assert bot_id is not None
            action = operation.removeprefix("workspace_subscription_")
            if action == "list":
                return {"bot_id": bot_id, "subscriptions": self.subscriptions.list(bot_id)}
            if action == "create":
                d = self.fields(
                    data,
                    {"connection_id", "operation_id", "params_json", "interval_s", "predicate_json"},
                    {"connection_id", "operation_id", "params_json", "interval_s"},
                )
                return {
                    "bot_id": bot_id,
                    **self.subscriptions.create(
                        bot_id,
                        d["connection_id"],
                        d["operation_id"],
                        self.unpack(d["params_json"]),
                        d["interval_s"],
                        self.unpack(d["predicate_json"]) if "predicate_json" in d else None,
                    ),
                }
            if action == "poll":
                d = self.fields(data, {"limit"})
                return {
                    "bot_id": bot_id,
                    "receipts": self.subscriptions.poll_due(bot_id=bot_id, limit=d.get("limit", 4), cancel=self._stop),
                }
            d = self.fields(data, {"subscription_id"}, {"subscription_id"})
            if action == "delete":
                return {"bot_id": bot_id, **self.subscriptions.delete(bot_id, d["subscription_id"])}
            if action in ("enable", "disable"):
                return {"bot_id": bot_id, **self.subscriptions.set_enabled(bot_id, d["subscription_id"], action == "enable")}
            raise ApiError(404, "not_found", "Unknown subscription action")
        if operation.startswith("workspace_browser_"):
            assert bot_id is not None
            action = operation.removeprefix("workspace_browser_")
            if action == "status":
                return {"bot_id": bot_id, **self.browser.status(bot_id)}
            if action == "act":
                result = self.browser.execute(bot_id, data, cancel=self._stop)
                return {"bot_id": bot_id, "action": data["action"], **result, "untrusted": True}
            if action == "artifact":
                if set(query) != {"artifact_id"}:
                    raise ApiError(400, "invalid", "An artifact id is required")
                return {"bot_id": bot_id, **self.browser.read_artifact(bot_id, query["artifact_id"]), "untrusted": True}
            self.fields(data, set())
            if action == "close":
                result = self.browser.close(bot_id)
            elif action == "reset":
                result = self.browser.reset(bot_id)
            else:
                raise ApiError(404, "not_found", "Unknown browser action")
            return {"bot_id": bot_id, **(result or {}), "closed": True, **({"reset": True} if action == "reset" else {})}
        if operation.startswith("workspace_connection_"):
            assert bot_id is not None
            action = operation.removeprefix("workspace_connection_")
            if action == "list":
                return {"bot_id": bot_id, "connections": self.connections.list(bot_id)}
            if action == "authorize":
                d = self.fields(data, {"provider_id", "operation_ids"}, {"provider_id", "operation_ids"})
                return {"bot_id": bot_id, **self.connections.start(bot_id, d["provider_id"], d["operation_ids"])}
            allowed = {"connection_id", "operation_id", "input_json"} if action == "invoke" else {"connection_id"}
            d = self.fields(data, allowed, allowed)
            if action == "invoke":
                return {
                    "bot_id": bot_id,
                    **self.connections.invoke(bot_id, d["connection_id"], d["operation_id"], self.unpack(d["input_json"])),
                    "untrusted": True,
                }
            if action not in ("get", "refresh", "disconnect"):
                raise ApiError(404, "not_found", "Unknown connection action")
            return {"bot_id": bot_id, **getattr(self.connections, action)(bot_id, d["connection_id"])}
        if operation.startswith("workspace_memory_"):
            assert bot_id is not None
            action = operation.removeprefix("workspace_memory_")
            if action == "read":
                return self.memory.read(bot_id)
            if action == "revisions":
                if set(query) - {"limit", "before"}:
                    raise ApiError(400, "invalid", "Unknown memory query field")
                try:
                    limit = int(query.get("limit", "20"))
                    before = int(query["before"]) if "before" in query else None
                except (ValueError, TypeError):
                    raise ApiError(400, "invalid", "Memory paging values must be integers") from None
                return self.memory.revisions(bot_id, limit=limit, before=before)
            if action == "edit":
                d = self.fields(data, {"text", "expected_revision"}, {"text", "expected_revision"})
                with self.service.bots._lock:
                    self.require_paused(bot_id)
                    return self.memory.edit(bot_id, d["text"], expected_revision=d["expected_revision"], actor="owner")
            if action == "restore":
                d = self.fields(data, {"revision", "expected_revision"}, {"revision", "expected_revision"})
                with self.service.bots._lock:
                    self.require_paused(bot_id)
                    return self.memory.restore(bot_id, d["revision"], expected_revision=d["expected_revision"], actor="owner")
        if operation.startswith("workspace_team_"):
            from ..bots.portability import PortabilityManager

            teams = PortabilityManager(self.service, memory=self.memory)
            action = operation.removeprefix("workspace_team_")
            if action == "imports":
                return teams.list_imports()
            if action == "inspect":
                d = self.fields(data, {"import_id"}, {"import_id"})
                return teams.inspect_import(d["import_id"])
            if action == "recover":
                d = self.fields(
                    data, {"import_id", "expected_recovery_sha256", "action"}, {"import_id", "expected_recovery_sha256", "action"}
                )
                return teams.recover_import(d["import_id"], expected_recovery_sha256=d["expected_recovery_sha256"], action=d["action"])
            if action == "templates":
                return teams.list_templates()
            if action == "template":
                if set(query) != {"template_id"}:
                    raise ApiError(400, "invalid", "A template id is required")
                return teams.get_template(query["template_id"])
            if action == "export":
                d = self.fields(data, {"bot_ids", "name", "kind", "include_memory"}, {"bot_ids", "name", "kind"})
                return teams.export_bundle(d["bot_ids"], name=d["name"], kind=d["kind"], include_memory=d.get("include_memory", False))
            d = self.fields(
                data,
                {"bundle_json", "conflict", "rename_agents_json", "rename_bots_json", "include_memory", "expected_plan_sha256"},
                {"bundle_json"},
            )
            kwargs = {"conflict": d.get("conflict", "reject"), "include_memory": d.get("include_memory", False)}
            for field in ("rename_agents", "rename_bots"):
                if field + "_json" in d:
                    kwargs[field] = self.unpack(d[field + "_json"])
            self.unpack(d["bundle_json"])
            bundle = d["bundle_json"]
            if action == "preview":
                if "expected_plan_sha256" in d:
                    raise ApiError(400, "invalid", "Preview does not accept a prior plan")
                return teams.preview_import(bundle, **kwargs)
            if action == "import":
                return teams.import_bundle(bundle, expected_plan_sha256=d.get("expected_plan_sha256"), **kwargs)
        raise ApiError(404, "not_found", "Unknown bot workspace operation")

    def require_paused(self, bot_id: str) -> None:
        from .service import ApiError

        bot = self.service.bots._require(bot_id)
        active = self.service.store.get_run(bot["active_run_id"]) if bot.get("active_run_id") else None
        group = self.service.store.get_group(bot["group_reservation"]) if bot.get("group_reservation") else None
        if (
            bot["status"] != "paused"
            or (active and active["status"] in ("queued", "running"))
            or (group and group["status"] in ("queued", "running"))
            or self.service.store.count_messages(bot_id, pending_only=True)
        ):
            raise ApiError(409, "conflict", "Stop the bot and wait for its active and queued work before editing memory")
