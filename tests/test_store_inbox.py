"""Inbox lookup must remain complete after years of retained message history."""

from __future__ import annotations

import json
import sqlite3

from echo_adk.api.store import Store


def message(index: int, *, role: str = "user", state: str = "pending", bot_id: str = "bot") -> dict:
    return {"message_id": f"{index:032x}", "bot_id": bot_id, "role": role, "state": state, "text": str(index)}


def test_pending_inbox_remains_visible_after_large_history_and_pagination_is_unchanged(tmp_path):
    store = Store(tmp_path)
    history_size = 100_001
    try:
        # One transaction and bulk insert keep the large-history regression inexpensive.
        with store.transaction():
            store._db.executemany(
                "INSERT INTO messages(message_id,bot_id,seq,role,state,doc) VALUES (?,?,?,?,?,?)",
                (
                    (f"{index:032x}", "bot", index, "user", "done", json.dumps(message(index, state="done")))
                    for index in range(1, history_size + 1)
                ),
            )
            store._db.execute("INSERT OR REPLACE INTO counters(name,value) VALUES ('seq',?)", (history_size,))
        first, second = message(history_size + 1), message(history_size + 2)
        for doc in (first, second, message(history_size + 3, role="system"), message(history_size + 4, state="running")):
            store.put_message(doc)
        store.put_message(message(history_size + 5, bot_id="other"))

        assert store.count_messages("bot") == history_size + 4
        assert store.count_messages("bot", pending_only=True) == 2
        assert store.pending_messages("bot") == [first, second]
        assert store.list_messages("bot", since=f"{history_size:032x}", limit=1) == [first]
        assert store.list_messages("bot", since=first["message_id"], limit=1) == [second]
        assert store.list_messages("other", since=first["message_id"], limit=1) == []

        first["state"] = "running"
        store.put_message(first)
        assert store.count_messages("bot", pending_only=True) == 1
        assert store.pending_messages("bot") == [second]
    finally:
        store.close()


def test_legacy_message_projection_migrates_transactionally_and_survives_reopen(tmp_path):
    database = sqlite3.connect(tmp_path / "echo-adk.sqlite3")
    database.execute("CREATE TABLE messages(message_id TEXT PRIMARY KEY,bot_id TEXT NOT NULL,seq INTEGER NOT NULL,doc TEXT NOT NULL)")
    records = [message(1, state="done"), message(2), message(3, role="bot"), message(4, state="running")]
    database.executemany(
        "INSERT INTO messages(message_id,bot_id,seq,doc) VALUES (?,?,?,?)",
        [(doc["message_id"], doc["bot_id"], index, json.dumps(doc)) for index, doc in enumerate(records, 1)],
    )
    database.commit()
    database.close()

    store = Store(tmp_path)
    try:
        assert store.count_messages("bot") == 4
        assert store.count_messages("bot", pending_only=True) == 1
        assert store.pending_messages("bot") == [records[1]]
        assert store.list_messages("bot", since=records[0]["message_id"], limit=2) == records[1:3]
        records[1]["state"] = "done"
        store.put_message(records[1])
    finally:
        store.close()

    reopened = Store(tmp_path)
    try:
        assert reopened.count_messages("bot", pending_only=True) == 0
        assert reopened.pending_messages("bot") == []
    finally:
        reopened.close()
