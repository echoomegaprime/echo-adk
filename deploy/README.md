# Deploying echo-adk-api on FORGE

`echo-adk-api` is a loopback-only service (`127.0.0.1:8378`) consumed by Echo Prime on the same host. It
needs: Python >= 3.10 (FORGE has 3.12.3), the Echo SDK gate on `127.0.0.1:8000`, and a loopback
OpenAI-compatible model endpoint (FORGE Qwen on `127.0.0.1:11441`) for the `qwen-local` lane.

## Install

```bash
# as forge
cd /home/forge
git clone https://github.com/echoomegaprime/echo-adk.git
cd echo-adk
python3 -m pip install --user -e .            # or run from the checkout: python3 -m echo_adk.cli ...
python3 -m echo_adk.cli validate agents/      # exit 0, "ok": true

# as root
install -d -o forge -g forge -m 0750 /var/lib/echo/echo-adk
install -m 0644 deploy/echo-adk-api.service /etc/systemd/system/echo-adk-api.service
install -d -m 0750 /etc/echo
# the ONLY place the gate key lives for this service; never in git, never in the unit
umask 077; printf 'ECHO_ADK_GATE_KEY=%s\n' "$(grep SOVEREIGN_KEY /home/forge/.echo_sovereign_key | cut -d= -f2)" > /etc/echo/echo-adk.env
chown root:forge /etc/echo/echo-adk.env; chmod 0640 /etc/echo/echo-adk.env
systemctl daemon-reload
systemctl enable --now echo-adk-api
```

Prefer a dedicated scoped SDK key over the sovereign key: mint one in `arcanum_sdk.sdk_keys` with
`scopes_json` limited to the read scopes the shipped agents use (`knowledge.read`, `docs.read`,
`caps.read`, `monitor.read`) and put that in `/etc/echo/echo-adk.env` instead.

## Verify

```bash
curl -s http://127.0.0.1:8378/health
# {"ok": true, "service": "echo-adk-api", "profile": "ECHO_ADK", "version": "0.1.0", "agents": 2, "bots": 0, "runs_active": 0}
curl -s -X POST http://127.0.0.1:8378/runs -H 'Content-Type: application/json' \
  -d '{"agent":"forge-steward","prompt":"How is node forge?"}'
# {"run_id": "<32 hex>", "status": "queued"}
curl -s http://127.0.0.1:8378/runs/<run_id> | python3 -m json.tool   # steps, proof.verdict
journalctl -u echo-adk-api -n 50 --no-pager                            # JSON lines; ECHO_ADK_DEBUG=1 for more
ss -tlnp 'sport = :8378'                                               # 127.0.0.1 only
```

## Operate

| Need | Command |
|---|---|
| Restart after a code pull | `sudo systemctl restart echo-adk-api` |
| Extend the capability registry | drop `capabilities.json` (`{"capabilities":[...]}` or `{"caps":[<echo.caps.detail payloads>]}`) into `/var/lib/echo/echo-adk/` or set `ECHO_ADK_CAPS_FILE`, then restart |
| Change the model endpoint / name | `ECHO_ADK_MODEL_ENDPOINT` (loopback only) / `ECHO_ADK_MODEL_NAME` in the unit or env file |
| Raise/lower the service daily cap | `ECHO_ADK_DAILY_CAP_USD` (default 200) in `/etc/echo/echo-adk.env` |
| Ledger / proofs / memory | `/var/lib/echo/echo-adk/{ledger,proofs,memory}/`, SQLite `echo-adk.sqlite3` (WAL) |
| Rollback | `git -C /home/forge/echo-adk checkout <previous sha> && sudo systemctl restart echo-adk-api`; state is forward-compatible within 0.x |

Port 8378 is the contract default (Prime `adk.baseUrl`/`bots.baseUrl`). Register it in the FORGE port
registry before enabling so the port guard does not flag it (see the FORGE port-drift note in the fleet
memory).

## Placement

CPU-light: the service runs no inference itself; the `qwen-local` lane calls the already-running loopback
Qwen endpoint. `MemoryHigh=300M / MemoryMax=400M` keep it well under the FORGE gate budget (#31853).
