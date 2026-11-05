"""Stand-in for the ``openclaw`` CLI (used by ``CliTransport`` tests): records argv, prints a JSON result.

``FAKE_OPENCLAW_OUT`` names the file that receives ``sys.argv[1:]`` as JSON; ``FAKE_OPENCLAW_MODE`` is
``ok`` (default), ``fail`` (exit 1 with ``{ok:false, error}``) or ``hang`` (sleep past the timeout).
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    out = os.environ.get("FAKE_OPENCLAW_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(sys.argv[1:], handle, ensure_ascii=False)
    mode = os.environ.get("FAKE_OPENCLAW_MODE", "ok")
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "fail":
        print(json.dumps({"ok": False, "deliveryStatus": "failed", "error": "chat not found; token=abc123secretvalue"}))
        return 1
    print(json.dumps({"ok": True, "channel": "telegram", "messageId": "4242"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
