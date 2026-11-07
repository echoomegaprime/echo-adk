"""Selected installed skill guidance is applied and receipted, never a tool grant."""

import hashlib
import json

import pytest

from echo_adk.api.service import AdkService, ApiError
from echo_adk.core.runner import Runner
from echo_adk.core.skills import SkillLibrary, SkillValidationError
from tests.fakes import FakeBroker, ScriptedModel, final_action


def install(root, name, body):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Installed test guidance\n---\n{body}\n")


def test_installed_guidance_is_bounded_redacted_and_covered_by_the_run_receipt(tmp_path, scout, lease_for):
    install(tmp_path, "echo-researcher-power", "Use traceable sources. secret=private-example")
    library = SkillLibrary(tmp_path)
    text, receipt = library.context(scout.skills)
    assert "private-example" not in text
    assert receipt["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    model, broker = ScriptedModel([final_action("ready")]), FakeBroker()
    result = Runner(model, broker, skill_library=library).run(scout, "research", lease_for(scout))
    assert result.proof.verdict == "PASS"
    assert "Use traceable sources" in model.seen[0][1][0]["content"]
    assert result.steps[0].data["skill_context"] == receipt
    assert broker.calls == []  # guidance did not grant or start a capability
    (tmp_path / "escape-skill").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(SkillValidationError, match="leaves"):
        library.context(["escape-skill"])
    install(tmp_path, "large-skill", "x" * 64001)
    with pytest.raises(SkillValidationError, match="context bound"):
        library.context(["large-skill"])


def test_configured_missing_skill_fails_before_model_invocation(tmp_path, scout, lease_for):
    model = ScriptedModel([final_action("must not run")])
    result = Runner(model, FakeBroker(), skill_library=SkillLibrary(tmp_path)).run(scout, "research", lease_for(scout))
    assert result.status == "failed" and result.proof.verdict == "FAIL" and model.calls == 0
    assert result.steps[0].data["operation"] == "load_skills"


def test_bot_plugins_apply_installed_guidance_through_queued_override(agents_copy, tmp_path):
    skills = tmp_path / "skills"
    install(skills, "echo-researcher-power", "Use primary sources.")
    install(skills, "review-helper", "Return the review checklist.")
    model = ScriptedModel([final_action("reviewed")])
    service = AdkService(
        agents_copy, tmp_path / "state", skills_dir=skills, model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False
    )
    try:
        bot = service.create_bot({"name": "Review", "agent": "knowledge-scout", "plugins": ["review-helper"]})
        receipt = service.message_bot(bot["bot_id"], {"text": "review"})
        run = service.execute_run(receipt["run_id"])
        assert run["proof"]["verdict"] == "PASS"
        assert "Return the review checklist" in json.dumps(model.seen)
        assert run["steps"][0]["data"]["skill_context"]["applied"] == ["echo-researcher-power", "review-helper"]
    finally:
        service.close()


def test_unconfigured_library_does_not_silently_accept_explicit_plugins(agents_copy, tmp_path, scout, lease_for):
    service = AdkService(agents_copy, tmp_path / "state", broker=FakeBroker(), start_scheduler=False)
    try:
        with pytest.raises(ApiError) as exc:
            service.create_bot({"name": "Needs installed skill", "agent": "knowledge-scout", "plugins": ["review-helper"]})
        assert exc.value.status == 503
        assert service.bots.list_bots() == {"bots": []}
    finally:
        service.close()
    model = ScriptedModel([final_action("ordinary task")])
    result = Runner(model, FakeBroker()).run(scout, "ordinary task", lease_for(scout))
    assert result.steps[0].data["skill_context"] == {"provider": "unconfigured", "declared": list(scout.skills), "applied": []}


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("bound", ["count", "size"])
def test_bot_config_validates_combined_skill_context_before_persisting(agents_copy, tmp_path, operation, bound):
    skills = tmp_path / "skills"
    agent_skills = [f"skill-{index}" for index in range(32)] if bound == "count" else ["base-skill"]
    for name in agent_skills:
        install(skills, name, "Guidance." if bound == "count" else "b" * 32000)
    install(skills, "extra-skill", "Additional guidance." if bound == "count" else "p" * 32000)
    model = ScriptedModel([final_action("done")])
    service = AdkService(
        agents_copy, tmp_path / "state", skills_dir=skills, model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False
    )
    try:
        service.create_agent(
            {
                "name": "combined-skill-worker",
                "role": "researcher",
                "skills": agent_skills,
                "capabilities": [],
                "lane": {"node": "forge", "model": "qwen-local"},
            }
        )
        service.validate_plugins(["extra-skill"])  # each fragment is valid by itself
        bot = None
        if operation == "update":
            bot = service.create_bot({"name": "Original", "agent": "combined-skill-worker"})
        with pytest.raises(ApiError) as error:
            if bot:
                service.bots.update_bot(bot["bot_id"], {"name": "Should not persist", "plugins": ["extra-skill"]})
            else:
                service.create_bot({"name": "Invalid combined context", "agent": "combined-skill-worker", "plugins": ["extra-skill"]})
        assert error.value.status == 400
        assert "32 installed skills" in str(error.value) if bound == "count" else "context bound" in str(error.value)
        assert service.store.list_runs() == [] and model.calls == 0
        if bot:
            unchanged = service.bots.get_bot(bot["bot_id"])
            assert unchanged["name"] == "Original" and unchanged["plugins"] == []
            service.bots.update_bot(bot["bot_id"], {"plugins": [agent_skills[0]]})
        else:
            assert service.bots.list_bots() == {"bots": []}
            bot = service.create_bot({"name": "Valid deduplicated context", "agent": "combined-skill-worker", "plugins": [agent_skills[0]]})
        # Reusing one of the agent's own skills must not double its count or
        # context size. The accepted boundary case still executes successfully.
        receipt = service.message_bot(bot["bot_id"], {"text": "Read the installed guidance"})
        run = service.execute_run(receipt["run_id"])
        assert run["proof"]["verdict"] == "PASS"
        assert run["steps"][0]["data"]["skill_context"]["applied"] == agent_skills
    finally:
        service.close()
