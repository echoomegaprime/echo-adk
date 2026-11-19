#!/usr/bin/env python3
"""Score the goal-loop run against the sealed ground-truth key.

Reads data/ground_truth_key.json (never read by the driver), state.json and
the drafts directory. Writes score.json and prints a summary.
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(BASE, "data", "ground_truth_key.json")) as f:
        key = json.load(f)
    with open(os.path.join(BASE, "data", "appointments.json")) as f:
        dataset = json.load(f)
    with open(os.path.join(BASE, "state.json")) as f:
        state = json.load(f)
    drafts_dir = os.path.join(BASE, "drafts")
    draft_files = sorted(fn for fn in os.listdir(drafts_dir) if fn.endswith(".md"))
    draft_ids = [fn[:-3] for fn in draft_files]

    per = {}
    correct = 0
    confusion = {}
    for appt in dataset["appointments"]:
        aid = appt["id"]
        truth = key[aid]
        entry = state["appointments"].get(aid, {})
        got = entry.get("decision")
        ok = got == truth
        correct += 1 if ok else 0
        confusion[f"{truth}->{got}"] = confusion.get(f"{truth}->{got}", 0) + 1
        per[aid] = {
            "truth": truth, "got": got, "correct": ok,
            "status": entry.get("status"), "run_id": entry.get("run_id"),
            "attempts": entry.get("attempts"),
            "quote_in_note": entry.get("quote_in_note"),
            "answered_model": entry.get("answered_model"),
            "draft_file": entry.get("draft_file"),
        }

    key_promise = sorted(a for a, v in key.items() if v == "PROMISE")
    model_promise = sorted(a for a, e in state["appointments"].items()
                           if e.get("decision") == "PROMISE")
    score = {
        "decisions_correct": correct,
        "decisions_total": len(key),
        "confusion": confusion,
        "key_promise": key_promise,
        "model_promise": model_promise,
        "draft_files": draft_ids,
        "drafts_match_model_promise_exactly": draft_ids == model_promise,
        "drafts_match_key_promise_exactly": draft_ids == key_promise,
        "duplicate_draft_files": len(draft_ids) != len(set(draft_ids)),
        "every_draft_has_source_note": all(
            "Source note (verbatim):" in open(os.path.join(drafts_dir, fn)).read()
            for fn in draft_files),
        "quotes_verified_in_note": {
            a: per[a]["quote_in_note"] for a in per},
        "per_appointment": per,
    }
    with open(os.path.join(BASE, "score.json"), "w") as f:
        json.dump(score, f, indent=2)
    print(json.dumps({k: score[k] for k in (
        "decisions_correct", "decisions_total", "confusion",
        "drafts_match_model_promise_exactly", "drafts_match_key_promise_exactly",
        "duplicate_draft_files", "every_draft_has_source_note")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
