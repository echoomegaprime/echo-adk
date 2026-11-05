"""Agent-Skills-spec loader and validator (https://agentskills.io/specification).

A skill is a directory whose only required file is ``SKILL.md`` with YAML frontmatter ``name``
(<= 64 chars, lowercase kebab-case, must match the directory name) and ``description`` (<= 1024 chars),
plus optional ``references/``, ``assets/``, ``scripts/``.

Progressive disclosure: L1 metadata (:func:`load_skill_meta`) -> L2 body (:func:`load_skill`) ->
L3 references (:meth:`LoadedSkill.reference`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .types import AGENT_NAME, Skill, ValidationError

MAX_SKILL_BYTES = 256 * 1024
MAX_REFERENCE_BYTES = 512 * 1024
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SkillValidationError(ValidationError):
    """The skill directory does not satisfy the Agent Skills constraints."""


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML frontmatter: ``key: value`` lines, quoted scalars, ``>``/``|`` folded blocks, flow lists."""
    match = _FRONTMATTER.match(text)
    if not match:
        raise SkillValidationError("SKILL.md must start with a --- frontmatter block")
    raw, body = match.group(1), match.group(2)
    data: dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise SkillValidationError(f"unexpected indentation in frontmatter: {line.strip()!r}")
        if ":" not in line:
            raise SkillValidationError(f"frontmatter line is not key: value: {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", key):
            raise SkillValidationError(f"invalid frontmatter key {key!r}")
        if key in data:
            raise SkillValidationError(f"duplicate frontmatter key {key!r}")
        if value in (">", "|", ">-", "|-"):
            block: list[str] = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            joiner = " " if value.startswith(">") else "\n"
            data[key] = joiner.join(part for part in block if part or joiner == "\n").strip()
            continue
        data[key] = _scalar(value)
    return data, body


def _scalar(value: str) -> Any:
    if value == "":
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"') if value[0] == '"' else inner
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_scalar(part.strip()) for part in inner.split(",")]
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.match(r"^-?\d+$", value):
        return int(value)
    if re.match(r"^-?\d+\.\d+$", value):
        return float(value)
    comment = value.find(" #")
    return value[:comment].strip() if comment > 0 else value


@dataclass(frozen=True)
class SkillMeta:
    """L1: what a host needs to decide whether to load the skill."""

    skill: Skill
    directory: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def description(self) -> str:
        return self.skill.description

    def to_dict(self) -> dict[str, Any]:
        return {**self.skill.to_dict(), "frontmatter": {k: v for k, v in self.frontmatter.items() if k not in ("name", "description")}}


@dataclass
class LoadedSkill:
    """L2 (body) with lazy L3 references."""

    meta: SkillMeta
    body: str

    @property
    def name(self) -> str:
        return self.meta.name

    def references(self) -> list[str]:
        root = self.meta.directory / "references"
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_file())

    def reference(self, name: str) -> str:
        """L3: read one file under ``references/``. Traversal and oversize files are refused."""
        if not _SAFE_NAME.match(name):
            raise SkillValidationError(f"invalid reference name {name!r}")
        path = (self.meta.directory / "references" / name).resolve()
        root = (self.meta.directory / "references").resolve()
        if root not in path.parents or not path.is_file():
            raise SkillValidationError(f"reference {name!r} not found")
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise SkillValidationError(f"reference {name!r} exceeds {MAX_REFERENCE_BYTES} bytes")
        return path.read_text(encoding="utf-8", errors="replace")

    def to_dict(self) -> dict[str, Any]:
        return {**self.meta.to_dict(), "body": self.body, "references": self.references()}


def load_skill_meta(directory: str | Path) -> SkillMeta:
    path = Path(directory)
    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        raise SkillValidationError(f"{path}: SKILL.md is missing")
    if skill_file.stat().st_size > MAX_SKILL_BYTES:
        raise SkillValidationError(f"{path}: SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
    frontmatter, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name:
        raise SkillValidationError(f"{path}: frontmatter name is required")
    if len(name) > 64 or not AGENT_NAME.match(name):
        raise SkillValidationError(f"{path}: name {name!r} must be lowercase kebab-case, 64 characters or fewer")
    if name != path.name:
        raise SkillValidationError(f"{path}: name {name!r} must match the directory name {path.name!r}")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError(f"{path}: frontmatter description is required")
    if len(description) > 1024:
        raise SkillValidationError(f"{path}: description is longer than 1024 characters")
    return SkillMeta(Skill(name, description.strip(), str(path)), path, frontmatter)


def load_skill(directory: str | Path) -> LoadedSkill:
    meta = load_skill_meta(directory)
    _, body = parse_frontmatter((meta.directory / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    return LoadedSkill(meta, body.strip())


def scan_skills(root: str | Path) -> tuple[list[SkillMeta], list[dict[str, str]]]:
    """Walk a skills directory. Returns ``(valid_metas, problems)``; never raises on one bad skill."""
    base = Path(root)
    if not base.is_dir():
        raise SkillValidationError(f"{base} is not a directory")
    metas: list[SkillMeta] = []
    problems: list[dict[str, str]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "SKILL.md").exists():
            continue
        try:
            metas.append(load_skill_meta(entry))
        except SkillValidationError as exc:
            problems.append({"skill": entry.name, "error": str(exc)})
    return metas, problems


def find_skill(roots: list[str | Path], name: str) -> Optional[SkillMeta]:
    for root in roots:
        candidate = Path(root) / name
        if (candidate / "SKILL.md").is_file():
            return load_skill_meta(candidate)
    return None
