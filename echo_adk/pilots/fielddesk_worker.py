"""Advance the fixed pilot using idempotent per-appointment ADK run resources."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from echo_adk.core.types import Envelope, Proof
from echo_adk.pilots.fielddesk_jobs import AGENT, BASE, MODEL, Jobs
from echo_adk.pilots.fielddesk_server import local_opener, read_token


def decision(item, appointment, run):
    proof = run.get("proof") or {}
    steps = run["steps"]
    if (
        run.get("run_id") != item["run_id"]
        or proof.get("run_id") != item["run_id"]
        or run.get("agent") != AGENT
        or proof.get("verdict") != "PASS"
        or proof.get("actions") != []
        or run["spent"]["cost_usd"] != 0
        or proof.get("events_sha256") != Proof.hash_events(Envelope.from_dict(s) for s in steps)
    ):
        raise ValueError("worker proof did not pass")
    models = [step["data"]["answered"] for step in steps if step["kind"] == "model"]
    if not models or any(m.get("provider") != "qwen-local" or m.get("model") != MODEL for m in models):
        raise ValueError("unexpected model provenance")
    answers = [step["data"]["text"] for step in steps if step["kind"] == "final"]
    if len(answers) != 1:
        raise ValueError("missing terminal answer")
    answer = json.loads(answers[0])
    label, quote, draft = answer["decision"], answer["source_quote"], answer["draft"]
    if label not in {"PROMISE", "NONE", "AMBIGUOUS"} or not isinstance(quote, str) or not quote.strip() or quote not in appointment["note"]:
        raise ValueError("decision lacks a verbatim source quote")
    if not isinstance(draft, str) or (label == "PROMISE") != bool(draft.strip()):
        raise ValueError("draft and decision disagree")
    return {
        "appointment_id": item["appointment"],
        "decision": label,
        "source_note": appointment["note"],
        "deciding_quote": quote,
        "draft": draft.strip() or None,
        "run_id": run["run_id"],
        "events_sha256": proof["events_sha256"],
        "model": MODEL,
    }


def advance(jobs, fetch):
    item = jobs.next_item()
    if item is None:
        return
    run = fetch(item["goal"], item["appointment"])
    if run["status"] in {"queued", "running", "waiting_for_seat"}:
        return
    if run["status"] != "done":
        jobs.finish(item, error=f"ADK run {run['status']}: {(run.get('error') or {}).get('code', 'no code')}")
        return
    try:
        result = decision(item, jobs.items[item["appointment"]], run)
    except (ValueError, KeyError, TypeError) as e:
        jobs.finish(item, error=f"unusable ADK receipt: {type(e).__name__}: {e}")
        return
    jobs.finish(item, result=result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    jobs = Jobs(args.state_dir, args.fixture)
    auth = read_token(args.token_file).decode()
    opener = local_opener()

    def fetch(goal, appointment):
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}{BASE}/{goal}/items/{appointment}/run",
            method="PUT",
            data=b"{}",
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        with opener.open(req, timeout=8) as f:
            return json.load(f)

    failures = 0
    while True:
        try:
            advance(jobs, fetch)
            failures = 0
        except (OSError, urllib.error.URLError) as exc:
            failures += 1
            if failures == 1 or failures % 30 == 0:
                print(f"pilot transport pending: {type(exc).__name__}", flush=True)
        time.sleep(min(5, 1 + failures))


if __name__ == "__main__":
    main()
