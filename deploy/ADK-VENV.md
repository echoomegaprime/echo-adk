# Google ADK adapter runtime (FORGE)

The echo-adk-api service runs the Echo core on the system Python, which has
no google-adk installed. The Google ADK adapter (echo_adk/adk) is an optional
extra (`google-adk>=2.13,<3`) and runs from a dedicated virtualenv so the
service environment stays standard-library only.

## Recreate the venv

```bash
cd /home/forge/echo-adk
python3 -m venv .venv-adk
.venv-adk/bin/pip install -e ".[adk]"
```

`.venv-adk` is a local runtime artifact. Do not commit it.

## Verify the adapter

```bash
.venv-adk/bin/python -c "
from echo_adk.agents import load_agent
from echo_adk.core.broker import GateBroker
from echo_adk.core.models import build_model
from echo_adk.adk import app
agent = load_agent('agents/knowledge-scout.json')
application = app(agent, model=build_model(agent.lane.model), broker=GateBroker())
print(type(application).__name__)
"
```

Verified 2026-11-13: google-adk 2.14.1, the knowledge-scout definition builds
a `google.adk.apps.app.App` named `knowledge_scout`.
