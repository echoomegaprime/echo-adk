"""``echo-adk`` console script: run, validate, serve, card, mcp. Headless; JSON on stdout, logs on stderr."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .agents import load_agent, load_agents_dir
from .core.broker import GateBroker
from .core.ledger import Ledger
from .core.log import configure, get_logger
from .core.models import ProviderUnavailable, build_model, parse_models_config
from .core.policy import CapabilityRegistry
from .core.runner import Runner
from .core.skills import scan_skills
from .core.types import Budget, Lease, ValidationError, new_id

log = get_logger("cli")

DEFAULT_STATE_DIR = os.environ.get("ECHO_ADK_STATE_DIR") or str(Path.home() / ".echo" / "echo-adk")


def _registry(args: argparse.Namespace) -> CapabilityRegistry:
    registry = CapabilityRegistry.builtin()
    caps_file = getattr(args, "caps_file", None) or os.environ.get("ECHO_ADK_CAPS_FILE")
    if caps_file:
        registry.load_json(caps_file)
    return registry


def _models_config(args: argparse.Namespace) -> dict[str, Any]:
    raw = getattr(args, "models_config", None)
    if not raw:
        return parse_models_config(None)
    path = Path(raw)
    text = path.read_text(encoding="utf-8") if path.is_file() else raw
    return parse_models_config(json.loads(text))


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _state_dir(args: argparse.Namespace) -> str:
    """``--state-dir`` is a global option; ``run``/``serve``/``mcp`` also accept it after the subcommand (that one wins)."""
    return getattr(args, "serve_state_dir", None) or args.state_dir


def cmd_run(args: argparse.Namespace) -> int:
    agent = load_agent(args.agent)
    registry = _registry(args)
    config = _models_config(args)
    model = build_model(agent.lane.model, config)
    broker = GateBroker()
    ledger = Ledger(_state_dir(args))
    budget = agent.lane.budget
    if args.max_steps:
        import dataclasses

        agent = dataclasses.replace(agent, max_steps=args.max_steps)
    run_id = new_id()
    lease = Lease.mint(run_id, agent.capabilities, ttl_s=budget.max_wall_s, max_uses=budget.max_calls)
    result = Runner(model, broker, registry, ledger=ledger).run(
        agent, args.prompt, lease, run_id=run_id, budget=Budget.from_dict({}, defaults=budget)
    )
    _emit(result.to_dict() if args.full else result.proof.to_dict() if result.proof else {})
    if result.final_text and not args.full:
        sys.stderr.write(result.final_text + "\n")
    verdict = result.proof.verdict if result.proof else "UNKNOWN"
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


def cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.path)
    report: dict[str, Any] = {"agents": [], "problems": []}
    if target.is_file():
        try:
            report["agents"].append(load_agent(target).to_dict())
        except ValidationError as exc:
            report["problems"].append({"file": target.name, "error": str(exc)})
    else:
        try:
            for agent in load_agents_dir(target).values():
                report["agents"].append(agent.to_dict())
        except ValidationError as exc:
            report["problems"].append({"file": str(target), "error": str(exc)})
    registry = _registry(args)
    for agent in report["agents"]:
        unknown = [cap for cap in agent["capabilities"] if cap not in registry]
        if unknown:
            report["problems"].append({"file": agent["source"], "error": f"capabilities not in the registry (default-deny): {unknown}"})
    if args.skills_dir:
        metas, problems = scan_skills(args.skills_dir)
        report["skills"] = [m.to_dict() for m in metas]
        report["problems"].extend({"skill": p["skill"], "error": p["error"]} for p in problems)
    report["ok"] = not report["problems"]
    _emit(report)
    return 0 if report["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.server import is_loopback_host, serve
    from .api.service import AdkService

    if not is_loopback_host(args.host):
        sys.stderr.write(
            json.dumps({"error": "invalid", "message": f"echo-adk-api binds to loopback only; refused host {args.host!r}"}) + "\n"
        )
        return 2
    config = _models_config(args)
    service = AdkService(
        args.agents_dir,
        _state_dir(args),
        registry=_registry(args),
        model_factory=lambda model_id: build_model(model_id, config),
        workers=args.workers,
    )
    serve(service, args.host, args.port)
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    from .a2a import agent_card, to_echo_a2a

    agent = load_agent(args.agent)
    registry = _registry(args)
    if args.format == "echo-a2a":
        _emit(to_echo_a2a(agent, endpoint=args.base_url, registry=registry))
    else:
        _emit(agent_card(agent, base_url=args.base_url, registry=registry))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp.server import McpServer

    agent = load_agent(args.agent)
    broker = GateBroker()
    ledger = Ledger(_state_dir(args))
    McpServer(agent, broker=broker, registry=_registry(args), ledger=ledger).serve(sys.stdin, sys.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="echo-adk", description="Echo Agent Development Kit")
    parser.add_argument("--version", action="version", version=f"echo-adk {__version__}")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="ledger/proof/memory/state directory")
    parser.add_argument("--caps-file", default=None, help="JSON file of extra Capability records (default-deny registry extension)")
    parser.add_argument("--models-config", default=None, help="JSON models config (file or inline); credential fields are refused")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an agent once; prints the Proof (exit 0 PASS, 1 FAIL, 2 UNKNOWN)")
    run.add_argument("agent", help="agent file (.json or .yaml)")
    run.add_argument("prompt")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("--full", action="store_true", help="print the whole run (steps, proof, spent)")
    run.add_argument("--state-dir", dest="serve_state_dir", default=None, help="same as the global --state-dir")
    run.set_defaults(func=cmd_run)

    validate = sub.add_parser("validate", help="validate an agents directory (or one file) and optional skills dir")
    validate.add_argument("path")
    validate.add_argument("--skills-dir", default=None)
    validate.set_defaults(func=cmd_validate)

    serve_p = sub.add_parser("serve", help="serve the frozen echo-adk-api contract on loopback")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8378)
    serve_p.add_argument("--agents-dir", default="agents")
    serve_p.add_argument("--workers", type=int, default=4)
    serve_p.add_argument("--state-dir", dest="serve_state_dir", default=None, help="same as the global --state-dir")
    serve_p.set_defaults(func=cmd_serve)

    card = sub.add_parser("card", help="print the agent card (A2A by default, or echo-a2a/1.0)")
    card.add_argument("agent")
    card.add_argument("--base-url", default="http://127.0.0.1:8378")
    card.add_argument("--format", choices=("a2a", "echo-a2a"), default="a2a")
    card.set_defaults(func=cmd_card)

    mcp = sub.add_parser("mcp", help="serve one agent's capabilities as MCP tools over stdio")
    mcp.add_argument("agent")
    mcp.add_argument("--state-dir", dest="serve_state_dir", default=None, help="same as the global --state-dir")
    mcp.set_defaults(func=cmd_mcp)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    configure()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProviderUnavailable as exc:
        sys.stderr.write(json.dumps({"error": "unavailable", "provider": exc.provider, "message": str(exc)}) + "\n")
        return 1
    except ValidationError as exc:
        sys.stderr.write(json.dumps({"error": "invalid", "message": str(exc)}) + "\n")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
