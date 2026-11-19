from __future__ import annotations

import json

import pytest

from echo_adk.core.models import (
    SPECS,
    CliModel,
    LoopbackModel,
    ProviderUnavailable,
    RunResult,
    build_model,
    child_env,
    parse_models_config,
    validate_endpoint,
)
from echo_adk.core.types import ValidationError
from tests.fakes import FakeHttpServer, openai_responder


def test_child_env_allowlist_drops_secret_shaped_variables():
    source = {
        "PATH": "/bin",
        "HOME": "/h",
        "FOO_API_KEY": "x",
        "MY_TOKEN": "y",
        "AWS_SECRET": "z",
        "DB_PASSWORD": "p",
        "GIT_CREDENTIAL": "c",
        "LANG": "C",
    }
    env = child_env(source)
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C"}
    assert not any(k for k in env if "KEY" in k or "TOKEN" in k)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://forge-2:11441",
        "http://100.113.87.107:11441",
        "http://user:pw@127.0.0.1:11441",
        "http://127.0.0.1:11441/v1",
        "ftp://127.0.0.1",
        "http://127.0.0.1?x=1",
    ],
)
def test_validate_endpoint_refuses_non_loopback_or_credentialed(endpoint):
    with pytest.raises(ValidationError):
        validate_endpoint(endpoint)


def test_validate_endpoint_accepts_loopback_origins():
    assert validate_endpoint("http://127.0.0.1:11441") == "http://127.0.0.1:11441"
    assert validate_endpoint("http://localhost:11441/") == "http://localhost:11441"
    assert validate_endpoint("http://[::1]:11441") == "http://::1:11441".replace("http://::1", "http://::1")


def test_models_config_refuses_api_key_shaped_fields():
    for bad in (
        {"api_key": "x"},
        {"apiKey": "x"},
        {"token": "x"},
        {"cli": {"claude": {"model": "m", "secret": "s"}}},
        {"cli": {"claude": {"token": "t"}}},
    ):
        with pytest.raises(ValidationError):
            parse_models_config(bad)
    with pytest.raises(ValidationError):
        parse_models_config({"endpoint": "http://forge-2:11441"})
    with pytest.raises(ValidationError):
        parse_models_config({"cli": {"openai": {"model": "gpt"}}})
    good = parse_models_config({"endpoint": "http://127.0.0.1:11441", "model": "qwen3", "cli": {"gpt": {"model": "gpt-6-astra"}}})
    assert good["cli"] == {"codex": {"model": "gpt-6-astra"}}


def test_build_model_never_substitutes_a_provider():
    with pytest.raises(ValidationError, match="Unknown model provider"):
        build_model("gemini-cloud", {"endpoint": "http://127.0.0.1:11441"})
    assert isinstance(build_model("qwen-local", {"endpoint": "http://127.0.0.1:11441"}), LoopbackModel)
    assert build_model("gpt", {"endpoint": "http://127.0.0.1:11441"}).provider == "codex"


def test_loopback_model_completes_and_reports_served_model():
    with FakeHttpServer() as server:
        server.state["responder"] = openai_responder(['{"action":"final","text":"hi token=abcdef123456"}'])
        model = LoopbackModel(server.url, "qwen", timeout_s=5)
        reply = model.complete("system", [{"role": "user", "content": "q"}])
        assert reply.answered.model == "qwen-served" and reply.answered.reported is True and reply.answered.provider == "qwen-local"
        assert "[REDACTED]" in reply.text
        sent = server.requests[-1]["body"]
        assert sent["messages"][0] == {"role": "system", "content": "system"} and sent["stream"] is False
        assert "authorization" not in {k.lower() for k in server.requests[-1]["headers"]}
        assert model.models() == ["qwen"]


def test_loopback_model_failures_are_provider_unavailable_without_fallback():
    with FakeHttpServer() as server:
        server.state["status"], server.state["payload"] = 500, {"error": "boom"}
        model = LoopbackModel(server.url, "qwen", timeout_s=5)
        with pytest.raises(ProviderUnavailable) as exc:
            model.complete("s", [{"role": "user", "content": "q"}])
        assert exc.value.provider == "qwen-local" and "no fallback was used" in str(exc.value)
        server.state["status"], server.state["payload"] = 200, {"choices": []}
        with pytest.raises(ProviderUnavailable, match="incomplete"):
            model.complete("s", [{"role": "user", "content": "q"}])
    dead = LoopbackModel("http://127.0.0.1:1", "qwen", timeout_s=1)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        dead.complete("s", [{"role": "user", "content": "q"}])
    with pytest.raises(ValidationError):
        LoopbackModel("http://127.0.0.1:11441", "qwen-cloud")


def test_loopback_model_response_cap():
    with FakeHttpServer() as server:
        server.state["responder"] = openai_responder(["x" * 5000])
        model = LoopbackModel(server.url, "qwen", timeout_s=5, max_bytes=2048)
        with pytest.raises(ProviderUnavailable, match="limit exceeded"):
            model.complete("s", [{"role": "user", "content": "q"}])


def _runner(stdout: str, code: int = 0, *, timed_out: bool = False, seen: list | None = None):
    def run(cmd, args, *, cwd, env, input_text, timeout_s, max_bytes):
        if seen is not None:
            seen.append({"cmd": cmd, "args": list(args), "env": dict(env), "input": input_text, "cwd": cwd})
        return RunResult(code, stdout, "", timed_out)

    return run


def test_cli_model_claude_spec_and_parse():
    seen: list = []
    stdout = json.dumps(
        {"is_error": False, "result": '{"action":"final","text":"ok"}', "modelUsage": {"claude-fable-5-1": {"outputTokens": 9}}}
    )
    model = CliModel("claude", model="fable", runner=_runner(stdout, seen=seen))
    reply = model.complete("SYS", [{"role": "user", "content": "hello"}])
    assert reply.answered.model == "claude-fable-5-1" and reply.answered.reported and reply.answered.requested == "fable"
    call = seen[0]
    assert call["cmd"] == "claude" and call["args"][:3] == ["-p", "--output-format", "json"]
    assert "--system-prompt" in call["args"] and call["args"][call["args"].index("--system-prompt") + 1] == "SYS"
    assert call["args"][-2:] == ["--model", "fable"]
    assert call["input"] is not None and "hello" in call["input"] and "SYS" not in call["input"]
    assert not any(k for k in call["env"] if "KEY" in k.upper() or "TOKEN" in k.upper())


def test_cli_model_codex_grok_argon_specs():
    codex_out = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"action":"final","text":"c"}'}}),
        ]
    )
    reply = CliModel("gpt", runner=_runner(codex_out)).complete("S", [{"role": "user", "content": "x"}])
    assert reply.answered.provider == "codex" and reply.answered.reported is False and reply.answered.model is None
    seen: list = []
    CliModel("grok", runner=_runner(json.dumps({"text": "g", "modelUsage": {"grok-4": {"outputTokens": 2}}}), seen=seen)).complete(
        "S", [{"role": "user", "content": "x"}]
    )
    assert "--prompt-file" in seen[0]["args"] and seen[0]["input"] is None and "--max-turns" in seen[0]["args"]
    seen.clear()
    reply = CliModel("argon", runner=_runner(json.dumps({"status": "SUCCESS", "response": "a"}), seen=seen)).complete(
        "S", [{"role": "user", "content": "x"}]
    )
    assert seen[0]["cmd"] == "agy" and seen[0]["args"][:4] == ["--output-format", "json", "--mode", "plan"]
    assert any(a.startswith("-p=") and "S\n" in a for a in seen[0]["args"]) and reply.answered.requested == "gemini-4-argon-medium"
    assert SPECS["argon"].max_prompt_chars == 28000
    with pytest.raises(ProviderUnavailable, match="too large"):
        CliModel("argon", runner=_runner("{}")).complete("S", [{"role": "user", "content": "x" * 30000}])


def test_cli_model_failures_name_the_provider_and_never_fall_back():
    attempts: list = []
    model = CliModel("codex", runner=_runner(json.dumps({"type": "turn.failed"}), code=1, seen=attempts))
    with pytest.raises(ProviderUnavailable) as exc:
        model.complete("S", [{"role": "user", "content": "x"}])
    assert exc.value.provider == "codex" and "no fallback was used" in str(exc.value)
    assert len(attempts) == 1 and attempts[0]["cmd"] == "codex"  # exactly one spawn, no second provider
    with pytest.raises(ProviderUnavailable, match="timed out"):
        CliModel("grok", runner=_runner("", timed_out=True)).complete("S", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable, match="unreadable"):
        CliModel("claude", runner=_runner("not json")).complete("S", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable, match="could not start"):
        CliModel("codex", bin="echo-adk-definitely-missing-binary").complete("S", [{"role": "user", "content": "x"}])
    with pytest.raises(ValidationError):
        CliModel("openai")
    with pytest.raises(ValidationError):
        CliModel("claude", model="bad model!")
