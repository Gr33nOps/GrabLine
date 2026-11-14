"""Send one command to the owned, persistent real browser test session."""

from __future__ import annotations

import json
import time
import uuid

from common import ROOT


def command(op: str, *, timeout: float = 35, **options: object):
    directory = ROOT / "commands"
    directory.mkdir(exist_ok=True)
    identifier = uuid.uuid4().hex
    request = directory / (identifier + ".json")
    request.write_text(json.dumps({"op": op, **options}), encoding="utf-8")
    response = directory / (identifier + ".result.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.exists():
            result = json.loads(response.read_text(encoding="utf-8"))
            assert result["ok"], result
            return result.get("value")
        time.sleep(0.1)
    raise TimeoutError(f"No response to browser command {op}")
