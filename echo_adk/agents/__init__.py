"""Agent definition files: ``.json`` (full) and a strict minimal YAML subset for the spec section 7 shape.

The core has no YAML dependency. The YAML subset covers block mappings, block sequences of scalars,
flow sequences ``[a, b]`` and flow mappings ``{k: v}``, quoted/bare scalars, ints, floats, booleans,
null and ``#`` comments. Anchors, tags, multi-document streams and block scalars are refused.
``convert_yaml_to_json`` writes the canonical ``.json`` form.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..core.types import EchoAgent, ValidationError

__all__ = ["load_agent", "load_agents_dir", "parse_mini_yaml", "convert_yaml_to_json", "MiniYamlError"]

AGENT_SUFFIXES = (".json", ".yaml", ".yml")
MAX_AGENT_FILE_BYTES = 64 * 1024


class MiniYamlError(ValidationError):
    """Input is outside the supported YAML subset or malformed."""


# --------------------------------------------------------------------------- mini YAML


def _strip_comment(line: str) -> str:
    out = []
    quote: Optional[str] = None
    for idx, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and (idx == 0 or line[idx - 1] != "\\"):
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(token: str) -> Any:
    token = token.strip()
    if token == "" or token in ("null", "~", "Null", "NULL"):
        return None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        inner = token[1:-1]
        if token[0] == '"':
            try:
                return json.loads(token)
            except json.JSONDecodeError:
                return inner
        return inner.replace("''", "'")
    if token in ("true", "True", "TRUE", "yes", "Yes"):
        return True
    if token in ("false", "False", "FALSE", "no", "No"):
        return False
    if re.match(r"^[-+]?\d+$", token):
        return int(token)
    if re.match(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$", token):
        return float(token)
    if token[0] in "&*!|>%@`":
        raise MiniYamlError(f"unsupported YAML construct near {token!r}")
    return token


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: Optional[str] = None
    current: list[str] = []
    for ch in inner:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    if quote or depth:
        raise MiniYamlError("unbalanced flow collection")
    return parts


def _flow_key_value(item: str) -> tuple[str, str]:
    quote: Optional[str] = None
    depth = 0
    for idx, ch in enumerate(item):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (idx + 1 == len(item) or item[idx + 1] in " \t"):
            return item[:idx].strip(), item[idx + 1 :].strip()
    raise MiniYamlError(f"flow mapping entry {item!r} needs 'key: value'")


def _value(token: str) -> Any:
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        return [] if not inner else [_value(part) for part in _split_flow(inner)]
    if token.startswith("{") and token.endswith("}"):
        inner = token[1:-1].strip()
        out: dict[str, Any] = {}
        for item in _split_flow(inner) if inner else []:
            key, val = _flow_key_value(item)
            key = str(_scalar(key))
            if key in out:
                raise MiniYamlError(f"duplicate key {key!r}")
            out[key] = _value(val)
        return out
    if token.startswith(("[", "{")) or token.endswith(("]", "}")):
        raise MiniYamlError(f"malformed flow collection {token!r}")
    return _scalar(token)


def parse_mini_yaml(text: str) -> Any:
    if "\t" in text:
        raise MiniYamlError("tabs are not allowed for indentation")
    if text.lstrip().startswith("---"):
        text = text.lstrip()[3:]
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if raw.strip() == "...":
            break
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))
    if not lines:
        return {}
    value, pos = _parse_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise MiniYamlError(f"unexpected content: {lines[pos][1]!r}")
    return value


def _parse_block(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    if lines[pos][1].startswith("- "):
        return _parse_sequence(lines, pos, indent)
    return _parse_mapping(lines, pos, indent)


def _parse_sequence(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
        item = lines[pos][1][2:].strip()
        pos += 1
        if item == "":
            if pos < len(lines) and lines[pos][0] > indent:
                value, pos = _parse_block(lines, pos, lines[pos][0])
                out.append(value)
            else:
                out.append(None)
        elif re.match(r"^[^\[\{\"'].*?:(\s|$)", item):
            # "- key: value" starts a nested mapping whose further keys are indented under it
            key, val = _flow_key_value(item)
            mapping: dict[str, Any] = {}
            mapping[str(_scalar(key))] = _value(val) if val else None
            if pos < len(lines) and lines[pos][0] > indent and not lines[pos][1].startswith("- "):
                rest, pos = _parse_mapping(lines, pos, lines[pos][0])
                for k, v in rest.items():
                    if k in mapping:
                        raise MiniYamlError(f"duplicate key {k!r}")
                    mapping[k] = v
            out.append(mapping)
        else:
            out.append(_value(item))
    if pos < len(lines) and lines[pos][0] > indent:
        raise MiniYamlError(f"bad indentation near {lines[pos][1]!r}")
    return out, pos


def _parse_mapping(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while pos < len(lines) and lines[pos][0] == indent:
        text = lines[pos][1]
        if text.startswith("- "):
            raise MiniYamlError(f"sequence item where a mapping key was expected: {text!r}")
        key, val = _flow_key_value(text)
        key = str(_scalar(key))
        if key in out:
            raise MiniYamlError(f"duplicate key {key!r}")
        pos += 1
        if val == "":
            if pos < len(lines) and lines[pos][0] > indent:
                out[key], pos = _parse_block(lines, pos, lines[pos][0])
            else:
                out[key] = None
        else:
            out[key] = _value(val)
    if pos < len(lines) and lines[pos][0] > indent:
        raise MiniYamlError(f"bad indentation near {lines[pos][1]!r}")
    return out, pos


# --------------------------------------------------------------------------- loaders


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_AGENT_FILE_BYTES:
        raise ValidationError(f"{path.name}: agent file exceeds {MAX_AGENT_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def load_agent(path: str | Path, *, source: Optional[str] = None) -> EchoAgent:
    file = Path(path)
    if file.suffix.lower() not in AGENT_SUFFIXES:
        raise ValidationError(f"{file.name}: agent files must be .json, .yaml or .yml")
    if not file.is_file():
        raise ValidationError(f"{file}: not found")
    text = _read(file)
    if file.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{file.name}: not valid JSON ({exc.msg})") from None
    else:
        data = parse_mini_yaml(text)
    try:
        return EchoAgent.from_dict(data, source=source if source is not None else file.name)
    except ValidationError as exc:
        raise ValidationError(f"{file.name}: {exc}") from None


def load_agents_dir(directory: str | Path) -> dict[str, EchoAgent]:
    """Load every agent file in a directory (non-recursive). Duplicate names are refused."""
    base = Path(directory)
    if not base.is_dir():
        raise ValidationError(f"{base} is not a directory")
    agents: dict[str, EchoAgent] = {}
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in AGENT_SUFFIXES or entry.name.startswith("."):
            continue
        agent = load_agent(entry, source=entry.name)
        if agent.name in agents:
            raise ValidationError(f"{entry.name}: duplicate agent name {agent.name!r} (also in {agents[agent.name].source})")
        agents[agent.name] = agent
    return agents


def convert_yaml_to_json(path: str | Path, out: Optional[str | Path] = None) -> Path:
    """Write the canonical ``.json`` form of a YAML agent file next to it (or at ``out``)."""
    agent = load_agent(path)
    target = Path(out) if out else Path(path).with_suffix(".json")
    data = agent.to_dict()
    data.pop("source", None)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target
