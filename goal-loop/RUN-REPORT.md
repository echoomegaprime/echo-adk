# Echo Goal Loop — First Verified Run Report

**Date:** 2026-11-19 (America/Chicago) · **Ordered by:** Bob, 2026-11-19 ~04:28 CT ("Start the Echo goal-loop build")
**Goal given to Echo:** "Review today's FieldDesk appointments, find the customers I promised to follow up with, and draft each follow-up for my review."
**Verdict: PASS — 19/20 correct decisions against the sealed key (bar was ≥18/20), all other acceptance criteria met.**

## What was built

A goal loop with two parts, both running on FORGE with no interactive session attached:

1. **The worker — an Echo ADK agent.** New agent `lyra-test-goal-loop` (role `fielddesk-followup`, lane `qwen-local`, zero capabilities, created via `POST /agents`, HTTP 201; updated once via PUT during the run — see Failures). One ADK **run per appointment**: the run's model step reads the appointment note, classifies it (PROMISE / NONE / AMBIGUOUS), cites the deciding quote verbatim, and for PROMISE records writes the follow-up draft text. Every completed run carries an ADK Proof (all 20 final runs: verdict **PASS**).
2. **The driver — `goal_loop.py`.** Feeds appointments to the ADK one at a time, harvests each run's final answer, persists drafts idempotently (one file per appointment id, write-if-absent), and checkpoints state atomically (`state.json`, tmp+rename) after every transition. It has **no send path** — drafts are files for Bob's review. It never reads the ground-truth key; scoring is a separate program (`score.py`) run after completion.

Canonical home: the agent definition lives in the ADK's own registry (`/home/forge/echo-adk/agents/lyra-test-goal-loop.json`, written by the ADK API itself). Code + dataset checkpointed to the canonical repo **echoomegaprime/echo-adk**, branch `agent/lyra-goal-loop-20261119`, commit `58410b4dadbdfec8ac5b3e6bf97f1210774a64b9` (push verified via ls-remote; **not merged** — no PR opened). Working copies: FORGE `/home/forge/echo-goal-loop/`, VM `~/workspace/echo-goal-loop/`.

## Model used — and why

Every run was answered by **`c3po-code:echo-qwen38-abliterated-256k`** (27.3B Q4_K_M, local Ollama at FORGE `127.0.0.1:11441`, temperature 0), recorded in each run's `answered.model`. This was not a free choice: the ADK's `qwen-local` lane is service-configured to request exactly that model (confirmed in `echo_adk/core/models.py` and by the run records). Reconfiguring the production ADK service's model env for a proof run would have been a larger disturbance than a temporary model swap, so the lane was used as configured. The swap evicted `echo-prometheus:27b-256k` from Ollama for the run window; **after scoring, Prometheus was restored with `keep_alive: -1` and verified** (`/api/ps`: echo-prometheus:27b-256k loaded, 21.4 GB VRAM, ctx 262144, expiry year 2319, probe answered). `echo-workspace-web` was never restarted or touched.

## Dataset and sealed key

20 synthetic FieldDesk appointments for 2026-11-19 (`data/appointments.json`: mowing, sprinkler, fence, cleanup, cleaning and consult visits with realistic field notes). Ground truth sealed **before** any run: `data/ground_truth_key.json`, sha256 `f27696720b4891f2749fc0e2d57038d74a7f04cfaa65adb236f7250dd07fdeea` (hash verified identical on FORGE; the FORGE copy was chmod 444). Class balance: **8 PROMISE / 8 NONE / 4 AMBIGUOUS**, including deliberate traps — FD-07 (a promise left in a voicemail counts), FD-14 (we asked the *customer* to call *us* = NONE), FD-02 (sending a quote is a follow-up).

## Acceptance measurements (all verified from state.json / score.json / service reads)

| Criterion | Bar | Result |
|---|---|---|
| Correct follow-up decisions vs sealed key | ≥ 18/20 | **19/20** — PROMISE 8/8, NONE 8/8, AMBIGUOUS 3/4 |
| Source note attached to every draft | all | **Yes** — 9/9 drafts carry the verbatim source note and deciding quote; model quotes verified as exact substrings of the notes **20/20** |
| Interruption: kill worker mid-run, resume, no dupes/loss | exactly one draft per qualifying record, zero duplicates | **Met for the run's decisions** — draft set == model-PROMISE set exactly (9 files, one per record, zero duplicates, none lost). Against the *key's* PROMISE set the draft set differs by exactly the one misclassification (FD-17, below) |
| Zero messages sent | 0 | **0** — the loop contains no send mechanism; all 6 echoBots verified still `paused`, $0.00 spent, after the run |
| Works with the chat closed | no interactive dependency | **Yes** — driver ran under `setsid nohup` on FORGE across separate SSH sessions, and survived being SIGKILLed and relaunched |
| External AI providers | disabled | **None used** — only the local `qwen-local` lane; run cost $0.00 |

## Interruption test (as executed)

Driver invocation 1 started 04:32:51 CT. At **04:37 CT**, with 3 appointments done and FD-06's ADK run in flight, the driver was **SIGKILLed** (`kill -KILL`). State at kill: FD-02/03/04 done; FD-01, FD-05 failed (see below); FD-06 `run_started` with run_id `70e78a83…` recorded. After a fix (below), the driver was relaunched at 04:38:39 CT: it skipped completed records, **harvested FD-06's orphaned run** (which had failed server-side; per the resume rule that failure did not consume the retry budget, and a fresh run completed FD-06), retried the failed records, and finished at **04:50:07 CT: done=20, failed=0**. 26 ADK runs were started in total (20 finals + 6 retries). No draft was written twice — every draft's write mode recorded as a single first `written`, never `exists`.

## What failed, and how it was handled

1. **FD-01 cold-load failures (04:32–04:33 CT).** The first two runs failed `unavailable: local model endpoint is unreachable or timed out` — the base model was still loading into Ollama (evicting Prometheus) when the first calls raced it. Once warm (~50 s per call after), no further availability failures. FD-01 was retried on resume and classified correctly. *Lesson for the next version: the driver should pre-warm/verify the lane model before starting work.*
2. **Malformed model actions (FD-05, FD-06, first instruction version).** The ADK runner requires every model reply to be an action envelope — `{"action":"final","text":"…"}` — specified in the system prompt the runner appends *after* the agent instruction. My first instruction told the model to reply with a bare JSON object, contradicting the envelope; when the model followed my instruction literally, the runner rejected it (`denied: malformed model action: action must be cap or final`). FD-02/03/04 succeeded because the model used the envelope anyway. **Fix applied during the interruption window:** agent instruction rewritten (PUT, HTTP 200) to require the final-action envelope with a `DECISION:/QUOTE:/DRAFT:` line format inside `text`; the driver's parser updated to match (JSON kept as fallback). After the fix: zero malformed failures in 17 runs.
3. **The one wrong decision — FD-17 (AMBIGUOUS → PROMISE).** Note: *"Talked about the back lot and the drainage ditch. Follow up at some point."* The model treated "Follow up at some point" as a commitment and drafted a follow-up (quote cited verbatim: "Follow up at some point."). Under the sealed key this is ambiguous — no who/when/commitment — so it is scored wrong, and it is the sole reason the draft set (9) exceeds the key's qualifying set (8). The draft is preserved as `drafts/FD-17.md`, clearly a false positive for Bob to see. The other three ambiguous notes were correctly flagged with no drafts.

## State after the run

- FORGE Ollama: `echo-prometheus:27b-256k` loaded and pinned (restored, verified) — the workspace seat's expected state.
- echo-adk-api: healthy, 9 agents (incl. `lyra-test-goal-loop`), 6 bots all paused, 0 active runs. Bots were never resumed.
- `echo-workspace-web`: untouched throughout (another crew's repair was in progress; this build did not restart or reconfigure it).
- Artifacts: `state.json` (per-appointment run ids, decisions, quotes, attempts), `score.json` (full confusion matrix + per-appointment detail), `driver.log` (timestamped run journal), `drafts/FD-01.md … FD-08.md + FD-17.md` — in `~/workspace/echo-goal-loop/` and FORGE `/home/forge/echo-goal-loop/`.

## Exact resume point / what's next

The proof is **complete**; nothing is mid-flight. Natural next steps, none started: (1) open a PR for `agent/lyra-goal-loop-20261119` if Bob wants the loop in the ADK repo proper; (2) point the driver at a real FieldDesk export instead of synthetic data (same agent, same contract); (3) add a model pre-warm step and a PROMISE/AMBIGUOUS boundary example set to the instruction, targeting FD-17-class errors; (4) bank these 20 graded records as the seed of the "what has Echo learned" regression set for the future Echo-specific adapter milestone.

## Recovering this completed pilot

The loopback ADK API can serve the completed synthetic pilot to an authenticated owner workspace.
Set `ECHO_ADK_FIELDDESK_PROOF_DIR` on the ADK service to the retained proof directory when activating the readback.
`GET /pilots/fielddesk-followups/result` returns the immutable goal ID, score, and one record per synthetic
appointment with the decision, original source note, deciding quote, optional draft, ADK run ID, and events digest.
The response type `completed_pilot` identifies an earlier execution. Repeated reads of the same receipts are idempotent.

The reader verifies the original fixture, scorer output, checkpoint, draft bytes, and all 20 local ADK proof chains.
Missing inputs, unavailable runs, and digest mismatches return HTTP 503. This route accepts no path, prompt or
execution options. It reads no sealed answer key and issues no model or worker requests. Keep the ADK endpoint
loopback-only. The owner workspace must enforce seat admission before exposing these records.
