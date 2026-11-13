from __future__ import annotations

import json

import pytest

from echo_adk.agents import MiniYamlError, convert_yaml_to_json, load_agent, load_agents_dir, parse_mini_yaml
from echo_adk.core.skills import SkillValidationError, load_skill, load_skill_meta, parse_frontmatter, scan_skills
from echo_adk.core.types import ValidationError
from tests.conftest import AGENTS_DIR, ROOT


def make_skill(root, name, description="Does a thing.", body="# Body\nUse me.", dir_name=None, refs=None):
    folder = root / (dir_name or name)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    for ref, text in (refs or {}).items():
        (folder / "references").mkdir(exist_ok=True)
        (folder / "references" / ref).write_text(text, encoding="utf-8")
    return folder


def test_skill_progressive_load_l1_l2_l3(tmp_path):
    folder = make_skill(tmp_path, "echo-researcher-power", refs={"guide.md": "deep reference"})
    meta = load_skill_meta(folder)
    assert meta.name == "echo-researcher-power" and meta.description == "Does a thing."
    skill = load_skill(folder)
    assert skill.body.startswith("# Body") and skill.references() == ["guide.md"]
    assert skill.reference("guide.md") == "deep reference"
    with pytest.raises(SkillValidationError):
        skill.reference("../SKILL.md")
    with pytest.raises(SkillValidationError):
        skill.reference("missing.md")


def test_skill_validation_rules(tmp_path):
    with pytest.raises(SkillValidationError, match="match the directory"):
        load_skill_meta(make_skill(tmp_path, "alpha-skill", dir_name="other-dir"))
    with pytest.raises(SkillValidationError, match="kebab"):
        load_skill_meta(make_skill(tmp_path, "Not_Kebab"))
    with pytest.raises(SkillValidationError, match="1024"):
        load_skill_meta(make_skill(tmp_path, "long-desc", description="x" * 1025))
    with pytest.raises(SkillValidationError, match="64"):
        load_skill_meta(make_skill(tmp_path, "a" * 65))
    (tmp_path / "no-frontmatter").mkdir()
    (tmp_path / "no-frontmatter" / "SKILL.md").write_text("# nope", encoding="utf-8")
    metas, problems = scan_skills(tmp_path)
    assert [m.name for m in metas] == [] and len(problems) >= 4


def test_frontmatter_parser_handles_quotes_and_folded_blocks():
    data, body = parse_frontmatter('---\nname: "quoted-name"\ndescription: >\n  folded\n  text\ntags: [a, b]\n---\nbody here')
    assert data == {"name": "quoted-name", "description": "folded text", "tags": ["a", "b"]} and body == "body here"


def test_shipped_agents_load_and_match_yaml_example():
    agents = load_agents_dir(AGENTS_DIR)
    assert set(agents) == {"knowledge-scout", "forge-steward"}
    scout_yaml = load_agent(ROOT / "docs" / "examples" / "knowledge-scout.yaml")
    scout_json = agents["knowledge-scout"]
    assert scout_yaml.to_dict() | {"source": ""} == scout_json.to_dict() | {"source": ""}
    assert agents["forge-steward"].capabilities == ("echo.caps.search", "echo.monitor.health.node")


def test_mini_yaml_subset_and_refusals():
    doc = parse_mini_yaml(
        "# comment\nname: demo\nlist:\n  - one\n  - two\nnested:\n  key: value # trailing\n"
        "  flow: {a: 1, b: [x, 'y z']}\nflag: true\nnum: 1.5\nempty:\n"
    )
    assert doc == {
        "name": "demo",
        "list": ["one", "two"],
        "nested": {"key": "value", "flow": {"a": 1, "b": ["x", "y z"]}},
        "flag": True,
        "num": 1.5,
        "empty": None,
    }
    for bad in ("a: &anchor x", "a: !!str x", "a:\tb", "a: [1, 2", "a: 1\na: 2"):
        with pytest.raises(MiniYamlError):
            parse_mini_yaml(bad)


def test_convert_yaml_to_json_and_loader_errors(tmp_path):
    out = convert_yaml_to_json(ROOT / "docs" / "examples" / "knowledge-scout.yaml", tmp_path / "scout.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["name"] == "knowledge-scout" and "source" not in data
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid JSON"):
        load_agent(tmp_path / "bad.json")
    (tmp_path / "bad.json").unlink()
    (tmp_path / "dup.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate agent name"):
        load_agents_dir(tmp_path)
    with pytest.raises(ValidationError):
        load_agent(tmp_path / "missing.yaml")
