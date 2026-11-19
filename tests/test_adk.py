"""Google ADK adapter tests against a fake ``google.adk`` (module paths/signatures of google-adk 2.13.0)."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys

import pytest

from echo_adk.core.models import ProviderUnavailable
from tests import fake_google_adk
from tests.conftest import ROOT
from tests.fakes import FailingModel, FakeBroker, ScriptedModel, cap_action, final_action


@pytest.fixture
def adk():
    previous = fake_google_adk.install()
    for name in [m for m in list(sys.modules) if m == "echo_adk.adk" or m.startswith("echo_adk.adk.")]:
        sys.modules.pop(name)
    module = importlib.import_module("echo_adk.adk")
    assert module.ADK_AVAILABLE is True
    yield module
    fake_google_adk.uninstall(previous)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def collect(agen):
    return [item async for item in agen]


def test_core_never_imports_google_adk():
    code = (
        "import echo_adk, echo_adk.core.runner, echo_adk.api, echo_adk.mcp, echo_adk.a2a, echo_adk.cli, sys; "
        "assert not [m for m in sys.modules if m.startswith('google')], 'google imported'; print('clean')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "clean", result.stderr


def test_adk_import_guard_without_google(monkeypatch):
    code = (
        "import echo_adk.adk as a; assert a.ADK_AVAILABLE is False\n"
        "try:\n    a.EchoModel\nexcept ImportError as e:\n    print('guarded:', 'echo-adk[adk]' in str(e))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "guarded: True", result.stderr


def test_toolset_matches_core_registry_drift(adk, scout, registry):
    session = adk.GovernedSession(scout, broker=FakeBroker(), registry=registry)
    toolset = adk.EchoToolset(session)
    tools = run(toolset.get_tools())
    assert [t.name for t in tools] == [adk.tool_name(c) for c in scout.capabilities]
    assert toolset.capability_ids() == [cap.id for cap in session.policy.allowed_capabilities()]
    # a cap missing from the registry is never exposed (default-deny)
    import dataclasses

    widened = dataclasses.replace(scout, capabilities=scout.capabilities + ("echo.not.registered",))
    assert "echo_not_registered" not in adk.EchoToolset(adk.GovernedSession(widened, broker=FakeBroker(), registry=registry)).tool_names()
    filtered = adk.EchoToolset(session, tool_filter=["echo_caps_search"])
    assert [t.name for t in run(filtered.get_tools())] == ["echo_caps_search"]
    decl = tools[0]._get_declaration()
    assert (
        decl.name == "echo_knowledge_search"
        and decl.parameters.required == ["query"]
        and decl.parameters.properties["query"].type == "STRING"
    )


def test_tool_run_async_is_governed(adk, scout, registry, lease_for):
    broker = FakeBroker()
    session = adk.GovernedSession(scout, broker=broker, registry=registry, lease=lease_for(scout, scopes=["echo.caps.search"]))
    tools = {t.name: t for t in run(adk.EchoToolset(session).get_tools())}
    allowed = run(tools["echo_caps_search"].run_async(args={"q": "x"}, tool_context=None))
    assert allowed["verdict"] == "allowed" and allowed["untrusted"] is True and broker.calls == [("echo.caps.search", {"q": "x"})]
    gated = run(tools["echo_knowledge_get"].run_async(args={"doc_id": "d"}, tool_context=None))
    assert gated["verdict"] == "gated" and gated["needs"] == "lease scope echo.knowledge.get" and len(broker.calls) == 1
    proof = session.proof()
    assert proof.verdict == "UNKNOWN" and proof.actions[0]["cap"] == "echo.caps.search"


def test_echo_model_renders_tools_and_emits_function_calls(adk, scout, registry):
    from google.genai import types

    core = ScriptedModel([cap_action("echo.caps.search", q="health"), final_action("done")])
    session = adk.GovernedSession(scout, broker=FakeBroker(), registry=registry)
    model = adk.EchoModel(core, session=session)
    assert model.model == "echo/fake" and adk.EchoModel.supported_models() == [r"echo/.*"]
    decl = types.FunctionDeclaration(
        name="echo_caps_search",
        description="search caps",
        parameters=types.Schema(type="OBJECT", properties={"q": types.Schema(type="STRING")}),
    )
    request = fake_google_adk.LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="which caps mention health?")])],
        config=types.GenerateContentConfig(system_instruction="You are scout.", tools=[types.Tool(function_declarations=[decl])]),
    )
    responses = run(collect(model.generate_content_async(request)))
    assert len(responses) == 1
    call = responses[0].content.parts[0].function_call
    assert call.name == "echo_caps_search" and call.args == {"q": "health"}
    assert responses[0].custom_metadata["echo"]["allow_reroute"] is False and responses[0].model_version == "fake-1"
    system, messages = core.seen[0]
    assert (
        "You are scout." in system
        and "echo_caps_search" in system
        and messages == [{"role": "user", "content": "which caps mention health?"}]
    )
    request.contents.append(types.Content(role="model", parts=[types.Part(function_call=call)]))
    request.contents.append(
        types.Content(
            role="user", parts=[types.Part(function_response=types.FunctionResponse(name="echo_caps_search", response={"ok": True}))]
        )
    )
    final = run(collect(model.generate_content_async(request)))[0]
    assert final.content.parts[0].text == "done" and final.content.parts[0].function_call is None
    assert "untrusted data" in core.seen[1][1][-1]["content"]
    assert [e.kind for e in session.envelopes] == ["model", "model"]


def test_echo_model_propagates_provider_unavailable_without_retry(adk, scout, registry):
    from google.genai import types

    failing = FailingModel("codex")
    session = adk.GovernedSession(scout, broker=FakeBroker(), registry=registry)
    model = adk.EchoModel(failing, session=session)
    request = fake_google_adk.LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hi")])])
    with pytest.raises(ProviderUnavailable) as exc:
        run(collect(model.generate_content_async(request)))
    assert exc.value.provider == "codex" and failing.calls == 1
    assert session.envelopes[-1].kind == "model" and session.envelopes[-1].ok is False
    plugin = adk.EchoGovernancePlugin(session)
    assert run(plugin.on_model_error_callback(callback_context=None, llm_request=request, error=exc.value)) is None


def test_governance_plugin_hooks(adk, scout, registry, lease_for):
    broker = FakeBroker()
    session = adk.GovernedSession(scout, broker=broker, registry=registry, lease=lease_for(scout, scopes=["echo.caps.search"]))
    tools = {t.name: t for t in run(adk.EchoToolset(session).get_tools())}
    plugin = adk.EchoGovernancePlugin(session)
    assert run(plugin.before_tool_callback(tool=tools["echo_caps_search"], tool_args={"q": "x"}, tool_context=None)) is None
    blocked = run(plugin.before_tool_callback(tool=tools["echo_knowledge_search"], tool_args={"query": "x"}, tool_context=None))
    assert blocked["verdict"] == "gated" and blocked["untrusted"] is True

    class ForeignTool:
        name = "python_eval"

    foreign = run(plugin.before_tool_callback(tool=ForeignTool(), tool_args={}, tool_context=None))
    assert foreign["verdict"] == "denied" and "not an Echo capability tool" in foreign["data"]["error"]
    cleaned = run(
        plugin.after_tool_callback(tool=tools["echo_caps_search"], tool_args={}, tool_context=None, result={"token": "abc", "x": 1})
    )
    assert cleaned["token"] == "[REDACTED]" and cleaned["untrusted"] is True and cleaned["redacted"] == 1
    errored = run(
        plugin.on_tool_error_callback(tool=tools["echo_caps_search"], tool_args={}, tool_context=None, error=RuntimeError("boom"))
    )
    assert errored["verdict"] == "denied" and errored["capability"] == "echo.caps.search"
    run(plugin.after_run_callback(invocation_context=None))
    assert plugin.last_proof is not None and plugin.last_proof.verdict == "FAIL" and broker.calls == []


def test_app_builds_llm_agent_with_echo_parts(adk, scout, registry, tmp_path):
    from echo_adk.core.ledger import Ledger

    core = ScriptedModel([final_action("ok")])
    application = adk.app(scout, model=core, broker=FakeBroker(), registry=registry, ledger=Ledger(tmp_path))
    assert application.name == "knowledge_scout" and application.root_agent.name == "knowledge_scout"
    assert isinstance(application.root_agent.model, adk.EchoModel) and isinstance(application.root_agent.tools[0], adk.EchoToolset)
    assert isinstance(application.plugins[0], adk.EchoGovernancePlugin)
    assert application.root_agent.instruction == scout.instruction
    assert json.dumps(application.root_agent.tools[0].tool_names()) == json.dumps([adk.tool_name(c) for c in scout.capabilities])
