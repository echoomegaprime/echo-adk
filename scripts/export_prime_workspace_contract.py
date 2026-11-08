"""Generate Prime's matching versioned resource schemas from the ADK contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echo_adk.sdk.operations import SCHEDULE  # noqa: E402
from echo_adk.sdk.workspace import operations, owner_callback_contract  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = {
        row.name.removeprefix("bot."): {
            "method": row.method,
            "path": row.path,
            "schema": row.schema,
            "readOnly": row.read_only,
            "query": list(row.query_fields),
        }
        for row in operations()
    }
    rows["connection.callback"] = owner_callback_contract()
    args.output.write_text(
        "// Generated from echo-adk/echo_adk/sdk/{operations,workspace}.py. The owner callback is not an SDK capability.\n"
        + "export const BOT_WORKSPACE_CONTRACT = "
        + json.dumps(rows, ensure_ascii=False, indent=1)
        + " as const;\n"
        + "export const BOT_SCHEDULE_SCHEMA = "
        + json.dumps(SCHEDULE, indent=1)
        + " as const;\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
