#!/usr/bin/env python3
"""Echo goal-loop driver - first verified goal loop proof (2026-11-19).

Goal: "Review today's FieldDesk appointments, find the customers I promised
to follow up with, and draft each follow-up for my review."

Design:
- One Echo ADK agent run per appointment (agent: lyra-test-goal-loop,
  lane qwen-local on FORGE Ollama). The run's model step does the
  understanding and, for PROMISE records, writes the draft text.
- This driver feeds work, harvests each run's final JSON, persists drafts
  idempotently (one file per appointment id, write-if-absent), and
  checkpoints state atomically after every transition, so the process can
  be killed mid-run and resumed with no lost or duplicated work.
- A run that was in flight when the driver died is harvested on resume
  (its run_id is in the state file), never blindly re-started.
- The driver NEVER reads the ground-truth key. Scoring is separate.
- There is no send path anywhere in this loop: drafts are files for review.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DRAFTS = os.path.join(BASE, "drafts")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "driver.log")
ADK = "http://127.0.0.1:8378"
AGENT = "lyra-test-goal-loop"
POLL_S = 5
RUN_TIMEOUT_S = 700
MAX_ATTEMPTS = 3
DECISIONS = ("PROMISE", "NONE", "AMBIGUOUS")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def log(msg):
    line = f"{now()} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(state):
    atomic_write(STATE_PATH, json.dumps(state, indent=2))


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"appointments": {}}


def adk(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ADK + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def build_prompt(appt):
    return (
        "Goal: Review today's FieldDesk appointments, find the customers I "
        "promised to follow up with, and draft each follow-up for my review.\n\n"
        f"Appointment {appt['id']}\n"
        f"Customer: {appt['customer']}\n"
        f"Service: {appt['service']}\n"
        f"When: {appt['date']} {appt['time']}\n"
        f"Note: {appt['note']}\n\n"
        "Classify this one appointment and, if it is a PROMISE, draft the "
        "follow-up. Reply with only the JSON object from your instructions."
    )


def parse_final(text):
    """Parse the run's final text: line format first, JSON as fallback."""
    if not isinstance(text, str):
        raise ValueError("final text is not a string")
    m = re.search(r"DECISION:\s*(PROMISE|NONE|AMBIGUOUS)", text)
    if m:
        decision = m.group(1)
        q = re.search(r"QUOTE:\s*(.+)", text)
        quote = q.group(1).strip().strip('"') if q else ""
        draft = ""
        if decision == "PROMISE":
            md = re.search(r"DRAFT:\s*\n?(.*)", text, re.S)
            draft = md.group(1).strip() if md else ""
        return {"decision": decision, "source_quote": quote, "draft": draft}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no DECISION line and no JSON object in final text")
    obj = json.loads(text[start:end + 1])
    decision = str(obj.get("decision", "")).strip().upper()
    if decision not in DECISIONS:
        raise ValueError(f"bad decision: {decision!r}")
    return {
        "decision": decision,
        "source_quote": str(obj.get("source_quote", "")),
        "draft": str(obj.get("draft", "")),
    }


def write_draft(appt, parsed, run_id, answered_model):
    path = os.path.join(DRAFTS, appt["id"] + ".md")
    if os.path.exists(path):
        return path, "exists"
    body = (
        f"# Follow-up draft - {appt['id']} - {appt['customer']} ({appt['service']})\n\n"
        f"Status: DRAFT FOR REVIEW - NOT SENT\n"
        f"Appointment: {appt['date']} {appt['time']}\n"
        f"ADK run: {run_id}\n"
        f"Model: {answered_model}\n"
        f"Source note (verbatim): {appt['note']}\n"
        f"Deciding quote: {parsed['source_quote']}\n\n"
        f"---\n\n{parsed['draft'].strip()}\n"
    )
    atomic_write(path, body)
    return path, "written"


def harvest(run_id):
    """Poll an ADK run to completion; return its record."""
    deadline = time.time() + RUN_TIMEOUT_S
    while time.time() < deadline:
        rec = adk("GET", f"/runs/{run_id}")
        status = rec.get("status")
        if status in ("done", "failed", "gated"):
            return rec
        time.sleep(POLL_S)
    raise TimeoutError(f"run {run_id} did not finish in {RUN_TIMEOUT_S}s")


def final_text(rec):
    for step in rec.get("steps", []):
        if step.get("kind") == "final":
            return step.get("data", {}).get("text", "")
    return ""


def answered_model(rec):
    for step in rec.get("steps", []):
        if step.get("kind") == "model":
            a = step.get("data", {}).get("answered", {})
            if a.get("model"):
                return a["model"]
    return "unknown"


def process(appt, state):
    aid = appt["id"]
    entry = state["appointments"].setdefault(
        aid, {"status": "pending", "attempts": 0, "run_id": None})
    if entry["status"] == "done":
        return
    inherited = set()
    if entry.get("run_id"):
        # A run started by a previous (killed) driver invocation: harvest it,
        # and if it failed, that failure does not consume this invocation's
        # retry budget - fall through to one fresh run.
        inherited.add(entry["run_id"])
    while True:
        if entry.get("run_id"):
            run_id = entry["run_id"]
            log(f"{aid}: harvesting run {run_id} (attempt {entry['attempts']})")
        else:
            entry["attempts"] += 1
            resp = adk("POST", "/runs", {
                "agent": AGENT,
                "prompt": build_prompt(appt),
                "budget": {"max_calls": 4, "max_cost_usd": 0.0, "max_wall_s": 600},
            })
            run_id = resp["run_id"]
            entry["run_id"] = run_id
            entry["status"] = "run_started"
            save_state(state)
            log(f"{aid}: started run {run_id} (attempt {entry['attempts']})")
        rec = harvest(run_id)
        entry["run_status"] = rec.get("status")
        entry["proof_verdict"] = (rec.get("proof") or {}).get("verdict")
        entry["answered_model"] = answered_model(rec)
        if rec.get("status") != "done":
            log(f"{aid}: run {run_id} ended status={rec.get('status')} "
                f"error={rec.get('error')}")
            entry["run_id"] = None
            if run_id in inherited:
                inherited.discard(run_id)
                save_state(state)
                continue
            if entry["attempts"] >= MAX_ATTEMPTS:
                entry["status"] = "failed"
                save_state(state)
                return
            save_state(state)
            continue
        try:
            parsed = parse_final(final_text(rec))
        except Exception as exc:
            log(f"{aid}: could not parse final text: {exc}")
            entry["run_id"] = None
            if run_id in inherited:
                inherited.discard(run_id)
                save_state(state)
                continue
            if entry["attempts"] >= MAX_ATTEMPTS:
                entry["status"] = "failed"
                entry["error"] = f"parse: {exc}"
                save_state(state)
                return
            save_state(state)
            continue
        entry["decision"] = parsed["decision"]
        entry["source_quote"] = parsed["source_quote"]
        entry["quote_in_note"] = parsed["source_quote"].strip() in appt["note"]
        if parsed["decision"] == "PROMISE":
            path, how = write_draft(appt, parsed, run_id, entry["answered_model"])
            entry["draft_file"] = os.path.basename(path)
            entry["draft_write"] = how
        entry["status"] = "done"
        save_state(state)
        log(f"{aid}: done decision={parsed['decision']} "
            f"quote_in_note={entry['quote_in_note']} "
            f"model={entry['answered_model']}")
        return


def main():
    os.makedirs(DRAFTS, exist_ok=True)
    with open(os.path.join(DATA, "appointments.json")) as f:
        dataset = json.load(f)
    state = load_state()
    state.setdefault("goal", dataset["goal"])
    state.setdefault("started_at", now())
    save_state(state)
    log(f"driver start: {len(dataset['appointments'])} appointments, "
        f"agent={AGENT}")
    for appt in dataset["appointments"]:
        process(appt, state)
    done = sum(1 for e in state["appointments"].values() if e["status"] == "done")
    failed = sum(1 for e in state["appointments"].values() if e["status"] == "failed")
    log(f"driver finish: done={done} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
