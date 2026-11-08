from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from echo_adk.bots.memory import BotMemoryError, BotMemoryRevisions
from echo_adk.core.redact import redact, redact_value
from echo_adk.core.types import canonical_json

BOT_A, BOT_B = "a" * 32, "b" * 32


def test_notes_revisions_are_scoped_redacted_and_restore_is_a_new_audited_edit(tmp_path):
    memory = BotMemoryRevisions(tmp_path)
    try:
        assert memory.read(BOT_A)["revision"] == 0
        first = memory.edit(BOT_A, "Remember the deadline. password=private-test-value", expected_revision=0, reason="Owner notes")
        second = memory.edit(BOT_A, "New deadline.", expected_revision=1)
        restored = memory.restore(BOT_A, 1, expected_revision=2)
        assert first["scope"] == "bot:" + BOT_A and first["redacted"] == 1
        assert first["text"] == "Remember the deadline. password=[REDACTED]"
        assert restored["text"] == first["text"] and restored["revision"] == 3
        assert restored["sha256"] == first["sha256"] and restored["revision_sha256"] != first["revision_sha256"]
        assert restored["redacted"] == 0 and restored["untrusted"] is True
        assert memory.read(BOT_B)["text"] == ""
        with pytest.raises(BotMemoryError, match="not found"):
            memory.restore(BOT_B, 1, expected_revision=0)
        history = memory.revisions(BOT_A, limit=2)
        assert [row["revision"] for row in history["revisions"]] == [3, 2]
        assert history["next_before"] == 2 and "text" not in history["revisions"][0]
        assert memory.revisions(BOT_A, before=2)["revisions"][0]["revision"] == 1
        assert history["revisions"][0]["previous_revision_sha256"] == second["revision_sha256"]
        assert history["revisions"][0]["restored_from"] == 1
        rows = memory._db.execute("SELECT * FROM revisions WHERE bot_id=? ORDER BY revision", (BOT_A,)).fetchall()
        for row in rows:
            payload = dict(row)
            digest = payload.pop("revision_sha256")
            assert digest == hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        memory.close()
        memory = BotMemoryRevisions(tmp_path)
        assert memory.read(BOT_A) == restored
        assert memory.restore(BOT_A, 0, expected_revision=3)["text"] == ""
    finally:
        memory.close()
    for path in tmp_path.glob("bot-memory.sqlite3*"):
        assert b"private-test-value" not in path.read_bytes()


def test_note_compare_and_swap_is_atomic_across_independent_sqlite_connections(tmp_path):
    first, second = BotMemoryRevisions(tmp_path), BotMemoryRevisions(tmp_path)
    barrier = threading.Barrier(2)

    def edit(instance, text):
        barrier.wait()
        try:
            return instance.edit(BOT_A, text, expected_revision=0)
        except BotMemoryError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda args: edit(*args), [(first, "first writer"), (second, "second writer")]))
        success = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, BotMemoryError)]
        assert len(success) == len(failures) == 1
        assert failures[0].status == 409 and failures[0].code == "conflict"
        assert first.read(BOT_A) == second.read(BOT_A) == success[0]
        assert len(first.revisions(BOT_A)["revisions"]) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("text", ["x" * 16001, "\U0001f600" * 8193, "\x00not text", "\ud800", None])
def test_invalid_or_oversized_note_never_advances_revision(tmp_path, text):
    memory = BotMemoryRevisions(tmp_path)
    try:
        with pytest.raises(BotMemoryError):
            memory.edit(BOT_A, text, expected_revision=0)
        assert memory.read(BOT_A)["revision"] == 0
        with pytest.raises(BotMemoryError):
            memory.read("bot:" + BOT_A)
        with pytest.raises(BotMemoryError):
            memory.edit(BOT_A, "ok", expected_revision=True)
    finally:
        memory.close()


def test_revision_bound_preserves_journal_and_delete_is_exact_bot_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("echo_adk.bots.memory.MAX_REVISIONS", 2)
    memory = BotMemoryRevisions(tmp_path)
    try:
        memory.edit(BOT_A, "a1", expected_revision=0)
        before = memory.edit(BOT_A, "a2", expected_revision=1)
        memory.edit(BOT_B, "other bot", expected_revision=0)
        with pytest.raises(BotMemoryError) as full:
            memory.edit(BOT_A, "a3", expected_revision=2)
        assert full.value.code == "revision_limit" and memory.read(BOT_A) == before
        memory.delete(BOT_A)
        assert memory.read(BOT_A)["revision"] == 0 and memory.revisions(BOT_A)["revisions"] == []
        assert memory.read(BOT_B)["text"] == "other bot"
    finally:
        memory.close()


def test_redaction_is_idempotent_for_notes_exports_json_and_structured_fields():
    raw = 'password="test-secret"; api_key=another-test-value; Authorization: Bearer abcdefghijk'
    clean, count = redact(raw)
    assert count == 3
    assert redact(clean) == (clean, 0)
    serialized = json.dumps({"instruction": clean, "ref": "bot-1"})
    assert redact(serialized) == (serialized, 0)
    assert redact(json.dumps(serialized)) == (json.dumps(serialized), 0)
    encoded_raw = json.dumps({"instruction": 'password="synthetic-secret"'})
    encoded_clean, count = redact(encoded_raw)
    assert count == 1 and json.loads(encoded_clean)["instruction"] == 'password="[REDACTED]"'
    structured, count = redact_value({"access_token": "example-value", "description": raw})
    assert count == 4
    assert redact_value(structured) == (structured, 0)
