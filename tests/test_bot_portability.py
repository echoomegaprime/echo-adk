from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from echo_adk.api.service import AdkService, ApiError
from echo_adk.bots.portability import PortabilityError, PortabilityManager
from tests.fakes import FakeBroker, ScriptedModel, final_action


@pytest.fixture
def workspace(agents_copy, tmp_path):
    model = ScriptedModel([final_action("must not execute during import")])
    service = AdkService(agents_copy, tmp_path / "state", model_factory=lambda _: model, broker=FakeBroker(), start_scheduler=False)
    teams = PortabilityManager(service, memory=service.workspace.memory)
    yield service, teams, model
    service.close()


def bundle(teams, template="research-team"):
    return teams.get_template(template)["bundle_json"]


def apply(teams, raw, **kwargs):
    preview = teams.preview_import(raw, **kwargs)
    return teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"], **kwargs)


def test_actual_local_templates_import_paused_without_permissions_or_execution(workspace):
    service, teams, model = workspace
    catalog = teams.list_templates()["templates"]
    assert {item["template_id"] for item in catalog} == {"research-team", "task-decomposition"}
    assert all(item["permissions"] == "none" and item["model"] == "qwen-local" for item in catalog)
    result = apply(teams, bundle(teams))
    assert result["status"] == "imported" and len(result["bots"]) == 2
    assert result["reused_agents"] == []
    for item in result["bots"]:
        bot = service.bots.get_bot(item["bot_id"])
        assert bot["status"] == "paused" and bot["lease"]["scopes"] == []
        assert bot["schedule"] is None and bot["daily_cost_cap_usd"] == 0
        assert service.get_agent(bot["agent"]).capabilities == ()
    assert service.bots.tick() == 0 and service.store.list_runs() == [] and model.calls == 0


def test_department_roundtrip_is_a_paused_collection_without_group_execution(workspace):
    service, teams, model = workspace
    bot = service.create_bot({"name": "Department source", "agent": "forge-steward"})
    exported = teams.export_bundle([bot["bot_id"]], name="Research department", kind="department")
    assert exported["kind"] == "department" and json.loads(exported["bundle_json"])["kind"] == "department"
    result = apply(teams, exported["bundle_json"], conflict="reuse_identical", rename_bots={"bot-1": "Imported department bot"})
    created = service.bots.get_bot(result["bots"][0]["bot_id"])
    assert created["status"] == "paused" and created["lease"]["scopes"] == []
    assert service.store._db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    assert service.store.list_runs() == [] and model.calls == 0


def test_preview_shows_effective_permissions_schedule_and_zero_cost_default(workspace):
    service, teams, _ = workspace
    data = json.loads(bundle(teams))
    data["agents"][0]["capabilities"] = ["echo.caps.search"]
    data["bots"][0].pop("daily_cost_cap_usd")
    data["bots"][0]["schedule"] = {"cron": "0 9 * * 1-5", "tz": "UTC"}
    raw = json.dumps(data)
    preview = teams.preview_import(raw)
    assert preview["agents"][0]["capabilities"] == ["echo.caps.search"]
    assert preview["agents"][0]["skills"] == [] and preview["agents"][0]["lane"]["model"] == "qwen-local"
    assert preview["bots"][0]["schedule"]["cron"] == "0 9 * * 1-5"
    assert preview["bots"][0]["daily_cost_cap_usd"] == 0 and preview["bots"][0]["plugins"] == []
    assert preview["bots"][0]["lease_scopes"] == []
    installed = teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"])
    assert service.bots.get_bot(installed["bots"][0]["bot_id"])["daily_cost_cap_usd"] == 0


def test_export_excludes_runtime_authority_and_memory_unless_notes_explicitly_selected(workspace):
    service, teams, _ = workspace
    bot = service.create_bot({"name": "source", "agent": "forge-steward", "instruction": "password=synthetic-export-secret"})
    source = service.store.get_bot(bot["bot_id"])
    source.update(
        {"active_run_id": "a" * 32, "browser_session": "private-session", "grant_ref": "private-grant", "access_token": "private-token"}
    )
    service.store.put_bot(source)
    teams.memory.edit(bot["bot_id"], "Personal notes", expected_revision=0)
    exported = teams.export_bundle([bot["bot_id"]], name="My team")
    raw = exported["bundle_json"]
    for forbidden in (
        bot["bot_id"],
        "active_run_id",
        "browser_session",
        "private-session",
        "grant_ref",
        "access_token",
        '"lease"',
        "Personal notes",
        "synthetic-export-secret",
    ):
        assert forbidden not in raw
    document = json.loads(raw)
    assert document["bots"][0]["ref"] == "bot-1"
    assert document["bots"][0]["instruction"] == "password=[REDACTED]"
    assert set(document["agents"][0]) <= {"name", "role", "skills", "capabilities", "lane", "max_steps", "instruction"}
    selected = teams.export_bundle([bot["bot_id"]], name="Explicit notes", include_memory=True)
    assert json.loads(selected["bundle_json"])["bots"][0]["memory_text"] == "Personal notes"
    options = {"conflict": "reuse_identical", "rename_bots": {"bot-1": "Imported copy"}}
    result = apply(teams, selected["bundle_json"], **options)
    imported = service.bots.get_bot(result["bots"][0]["bot_id"])
    assert imported["lease"]["scopes"] == []
    assert teams.memory.read(imported["bot_id"])["text"] == ""
    assert any("excluded" in warning for warning in result["warnings"])


def test_memory_import_requires_its_own_opt_in_and_does_not_copy_revision_ids(workspace):
    service, teams, _ = workspace
    data = json.loads(bundle(teams))
    data["bots"][0]["memory_text"] = "Preferred style. token=synthetic-note-token"
    raw = json.dumps(data)
    preview = teams.preview_import(raw, include_memory=True)
    assert preview["bots"][0]["memory_included"] is True
    result = teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"], include_memory=True)
    first, second = result["bots"]
    notes = teams.memory.read(first["bot_id"])
    assert notes["revision"] == 1 and notes["text"] == "Preferred style. token=[REDACTED]"
    assert teams.memory.read(second["bot_id"])["revision"] == 0
    assert notes["scope"] == f"bot:{first['bot_id']}"
    assert service.bots.tick() == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc.update({"credentials": {"token": "forbidden"}}),
        lambda doc: doc["agents"][0].update({"source": "/untrusted/path"}),
        lambda doc: doc["agents"][0].update({"capabilities": ["echo.unregistered.example"]}),
        lambda doc: doc["agents"][0].update({"capabilities": ["echo.*"]}),
        lambda doc: doc["bots"][0].update({"lease": {"scopes": ["echo.caps.search"]}}),
        lambda doc: doc["bots"][0].update({"status": "idle"}),
        lambda doc: doc["bots"][0].update({"memory_scope": "bot:" + "f" * 32}),
        lambda doc: doc["bots"][1].update({"schedule": {"every_s": 1}}),
        lambda doc: doc["bots"][1].update({"ref": doc["bots"][0]["ref"]}),
    ],
)
def test_whole_bundle_validation_prevents_partial_writes(workspace, mutation):
    service, teams, model = workspace
    before = {path.name: path.read_bytes() for path in service.agents_dir.iterdir()}
    data = json.loads(bundle(teams))
    mutation(data)
    with pytest.raises((PortabilityError, ApiError)):
        teams.preview_import(json.dumps(data))
    assert before == {path.name: path.read_bytes() for path in service.agents_dir.iterdir()}
    assert service.store.list_bots() == [] and service.store.list_runs() == [] and model.calls == 0


def test_preview_hash_binds_definitions_and_conflicts_are_explicit(workspace):
    service, teams, _ = workspace
    raw = bundle(teams)
    preview = teams.preview_import(raw)
    changed = json.loads(raw)
    changed["agents"][0]["instruction"] = "Different plan"
    with pytest.raises(PortabilityError, match="preview changed"):
        teams.import_bundle(json.dumps(changed), expected_plan_sha256=preview["plan_sha256"])
    created = apply(teams, raw)
    with pytest.raises(PortabilityError) as conflict:
        teams.preview_import(raw)
    assert conflict.value.status == 409
    before = {name: (service.agents_dir / f"{name}.json").read_bytes() for name in created["created_agents"]}
    options = {"conflict": "reuse_identical", "rename_bots": {"bot-1": "Research copy", "bot-2": "Review copy"}}
    result = apply(teams, raw, **options)
    assert result["created_agents"] == [] and set(result["reused_agents"]) == set(before)
    assert before == {name: (service.agents_dir / f"{name}.json").read_bytes() for name in before}
    with pytest.raises(PortabilityError):
        teams.preview_import(json.dumps(changed), **options)
    with pytest.raises(PortabilityError, match="unknown"):
        teams.preview_import(raw, rename_bots={"not-a-ref": "oops"})


def test_concurrent_import_of_same_preview_cannot_create_duplicate_teams(workspace):
    service, teams, model = workspace
    raw = bundle(teams)
    preview = teams.preview_import(raw)

    def install(_):
        try:
            return teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"])
        except PortabilityError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(install, range(2)))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert len(service.store.list_bots()) == 2 and model.calls == 0
    assert len(teams.list_imports()["imports"]) == 1


def test_bot_creation_failure_rolls_back_new_resources_without_touching_existing(workspace, monkeypatch):
    service, teams, model = workspace
    before = {path.name: path.read_bytes() for path in service.agents_dir.iterdir()}
    original = service.create_bot
    count = 0

    def fail_second(body):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic storage failure")
        return original(body)

    monkeypatch.setattr(service, "create_bot", fail_second)
    with pytest.raises(OSError):
        apply(teams, bundle(teams))
    assert service.store.list_bots() == [] and model.calls == 0
    assert before == {path.name: path.read_bytes() for path in service.agents_dir.iterdir()}
    assert teams.list_imports()["imports"][0]["status"] == "rolled_back"


def test_interrupted_tracked_import_can_be_inspected_and_rolled_back_after_restart(agents_copy, tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = AdkService(agents_copy, state, broker=FakeBroker(), start_scheduler=False)
    teams = PortabilityManager(first, memory=first.workspace.memory)
    raw = bundle(teams)
    preview = teams.preview_import(raw)

    def crash(_):
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(first, "create_bot", crash)
    with pytest.raises(SystemExit):
        teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"])
    import_id = teams.list_imports()["imports"][0]["import_id"]
    first.close()
    second = AdkService(agents_copy, state, broker=FakeBroker(), start_scheduler=False)
    recovered = PortabilityManager(second, memory=second.workspace.memory)
    try:
        inspect = recovered.inspect_import(import_id)
        assert inspect["rollback_available"] is True and inspect["status"] == "prepared"
        result = recovered.recover_import(import_id, action="rollback", expected_recovery_sha256=inspect["recovery_sha256"])
        assert result["status"] == "rolled_back"
        assert second.store.list_bots() == []
        assert "research-team-researcher" not in second.agents
        assert "forge-steward" in second.agents
        assert apply(recovered, raw)["status"] == "imported"
    finally:
        second.close()


def test_unproven_crash_window_file_is_preserved_and_recovery_can_explicitly_release_hold(workspace, monkeypatch):
    service, teams, _ = workspace
    raw = bundle(teams)
    preview = teams.preview_import(raw)
    original = service.create_agent

    def crash_after_write(body):
        original(body)
        raise SystemExit("interrupted before ownership receipt")

    monkeypatch.setattr(service, "create_agent", crash_after_write)
    with pytest.raises(SystemExit):
        teams.import_bundle(raw, expected_plan_sha256=preview["plan_sha256"])
    monkeypatch.setattr(service, "create_agent", original)
    import_id = teams.list_imports()["imports"][0]["import_id"]
    inspect = teams.inspect_import(import_id)
    assert inspect["rollback_available"] is False and inspect["preserve_available"] is True
    with pytest.raises(PortabilityError) as held:
        apply(teams, bundle(teams, "task-decomposition"))
    assert held.value.code == "import_recovery_required"
    with pytest.raises(PortabilityError, match="unproven"):
        teams.recover_import(import_id, action="rollback", expected_recovery_sha256=inspect["recovery_sha256"])
    outcome = teams.recover_import(import_id, action="preserve", expected_recovery_sha256=inspect["recovery_sha256"])
    assert outcome["status"] == "recovered_preserved"
    assert "research-team-researcher" in service.agents
    assert apply(teams, bundle(teams, "task-decomposition"))["status"] == "imported"


def test_duplicate_json_and_nonfinite_numbers_are_rejected_without_mutation(workspace):
    _, teams, _ = workspace
    with pytest.raises(PortabilityError, match="duplicate"):
        teams.preview_import('{"format":"a","format":"b"}')
    raw = bundle(teams).replace('"max_cost_usd":0', '"max_cost_usd":NaN')
    with pytest.raises(PortabilityError, match="finite"):
        teams.preview_import(raw)


def test_note_failure_compensates_both_sqlite_stores_and_created_agent_files(workspace, monkeypatch):
    service, teams, _ = workspace
    monkeypatch.setattr(service.workspace, "has_bot_resources", lambda _: False)
    data = json.loads(bundle(teams))
    for bot in data["bots"]:
        bot["memory_text"] = "Selected portable notes"
    edit = teams.memory.edit
    count = 0

    def fail_second(bot_id, text, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic notes storage failure")
        return edit(bot_id, text, **kwargs)

    monkeypatch.setattr(teams.memory, "edit", fail_second)
    with pytest.raises(OSError):
        apply(teams, json.dumps(data), include_memory=True)
    assert service.store.list_bots() == []
    assert teams.memory._db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
    assert "research-team-researcher" not in service.agents
    assert teams.list_imports()["imports"][0]["status"] == "rolled_back"


def test_recovery_refuses_changed_notes_and_owner_attached_resources(workspace, monkeypatch):
    service, teams, _ = workspace
    journal = teams._journal
    monkeypatch.setattr(service.workspace, "has_bot_resources", lambda _: False)

    def interrupt_commit(record, status):
        if status == "imported":
            raise SystemExit("interrupted before final receipt")
        return journal(record, status)

    monkeypatch.setattr(teams, "_journal", interrupt_commit)
    with pytest.raises(SystemExit):
        apply(teams, bundle(teams))
    monkeypatch.setattr(teams, "_journal", journal)
    import_id = teams.list_imports()["imports"][0]["import_id"]
    original = teams.inspect_import(import_id)
    assert original["rollback_available"] is True
    bot_id = service.store.list_bots()[0]["bot_id"]
    monkeypatch.setattr(service.workspace, "has_bot_resources", lambda value: value == bot_id)
    attached = teams.inspect_import(import_id)
    assert attached["rollback_available"] is False and attached["recovery_sha256"] != original["recovery_sha256"]
    with pytest.raises(PortabilityError, match="changed"):
        teams.recover_import(import_id, action="rollback", expected_recovery_sha256=original["recovery_sha256"])
    monkeypatch.setattr(service.workspace, "has_bot_resources", lambda _: False)
    teams.memory.edit(bot_id, "An owner's later note", expected_revision=0)
    changed = teams.inspect_import(import_id)
    assert changed["rollback_available"] is False
    with pytest.raises(PortabilityError, match="unproven"):
        teams.recover_import(import_id, action="rollback", expected_recovery_sha256=changed["recovery_sha256"])
    assert service.bots.get_bot(bot_id)["status"] == "paused"
    assert teams.memory.read(bot_id)["text"] == "An owner's later note"


def test_recovery_will_not_remove_agent_used_by_a_subsequent_direct_run(workspace, monkeypatch):
    service, teams, model = workspace
    journal = teams._journal
    monkeypatch.setattr(service.workspace, "has_bot_resources", lambda _: False)

    def interrupt_commit(record, status):
        if status == "imported":
            raise SystemExit("interrupted before final receipt")
        return journal(record, status)

    monkeypatch.setattr(teams, "_journal", interrupt_commit)
    with pytest.raises(SystemExit):
        apply(teams, bundle(teams))
    monkeypatch.setattr(teams, "_journal", journal)
    import_id = teams.list_imports()["imports"][0]["import_id"]
    monkeypatch.setattr(service, "submit_run", lambda _: None)
    service.create_run({"agent": "research-team-researcher", "prompt": "Owner's later explicit request", "lease": {"scopes": []}})
    preview = teams.inspect_import(import_id)
    assert preview["rollback_available"] is False and model.calls == 0
    with pytest.raises(PortabilityError):
        teams.recover_import(import_id, action="rollback", expected_recovery_sha256=preview["recovery_sha256"])
    assert "research-team-researcher" in service.agents
