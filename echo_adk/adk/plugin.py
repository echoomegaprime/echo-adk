"""``EchoGovernancePlugin(BasePlugin)``: before_tool classification + lease, after_tool redaction +
``untrusted:true``, no silent retry on tool/model errors, Proof at ``after_run``."""

from __future__ import annotations

from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from ..core.log import get_logger
from ..core.policy import ALLOWED, Decision
from ..core.redact import redact, redact_value
from ..core.types import Proof
from .session import GovernedSession
from .toolset import EchoCapabilityTool

log = get_logger("adk.plugin")


class EchoGovernancePlugin(BasePlugin):
    def __init__(self, session: GovernedSession, *, name: str = "echo_governance") -> None:
        super().__init__(name=name)
        self.session = session
        self.last_proof: Optional[Proof] = None

    # --- tools -----------------------------------------------------------------
    async def before_tool_callback(self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any) -> Optional[dict[str, Any]]:
        if not isinstance(tool, EchoCapabilityTool):
            decision = Decision("denied", None, f"tool {getattr(tool, 'name', '?')!r} is not an Echo capability tool", code="denied")
            return self.session.refusal(decision, None).to_dict()  # R5: only registry-backed tools run
        decision = self.session.evaluate(tool.capability.id, tool_args or {})
        if decision.verdict != ALLOWED:
            return self.session.refusal(decision, tool.capability.id).to_dict()  # skips the tool
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        cleaned, redacted = redact_value(result if isinstance(result, dict) else {"result": result})
        if isinstance(cleaned, dict):
            cleaned["untrusted"] = True
            if redacted:
                cleaned["redacted"] = int(cleaned.get("redacted") or 0) + redacted
        return cleaned

    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, error: Exception
    ) -> Optional[dict[str, Any]]:
        decision = Decision(
            "denied", getattr(getattr(tool, "capability", None), "tier", None), f"tool error: {type(error).__name__}", code="denied"
        )
        log.warning("adk_tool_error", tool=getattr(tool, "name", "?"), error=type(error).__name__)
        return self.session.refusal(
            decision, getattr(getattr(tool, "capability", None), "id", None), extra={"detail": redact(str(error))[0][:500]}
        ).to_dict()

    # --- model -----------------------------------------------------------------
    async def on_model_error_callback(self, *, callback_context: Any, llm_request: Any, error: Exception) -> Any:
        # Returning None lets ADK surface the error. No retry, no reroute (R2).
        log.warning("adk_model_error", error=type(error).__name__, detail=redact(str(error))[0][:300])
        return None

    # --- run -------------------------------------------------------------------
    async def after_run_callback(self, *, invocation_context: Any) -> None:
        self.last_proof = self.session.proof()
        log.info("adk_run_proof", run_id=self.session.run_id, verdict=self.last_proof.verdict, actions=len(self.last_proof.actions))
