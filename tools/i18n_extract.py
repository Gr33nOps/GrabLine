#!/usr/bin/env python3
"""Extract the app's translatable strings and report catalog coverage.

Scans app/ for calls to the translation function ``t("...")`` and collects the
English source strings (AST-based, so implicit "a" "b" concatenation and
multi-line literals are handled). Then, for each app/i18n/<code>.json catalog,
reports how many of those strings are translated, missing, or stale.

  python tools/i18n_extract.py            # coverage report
  python tools/i18n_extract.py --pot      # print every source string as JSON
  python tools/i18n_extract.py --fill xx  # add missing keys (empty) to xx.json
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app"
CATALOGS = ROOT / "app" / "i18n"


def _is_t_call(func: ast.expr) -> bool:
    # t("...") translates now; N_("...") only marks a constant for extraction.
    return isinstance(func, ast.Name) and func.id in ("t", "N_", "_note")


def source_strings() -> set[str]:
    found: set[str] = set()
    for py in sorted(SRC.rglob("*.py")):
        if "tests" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_t_call(node.func) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
    return found


def main() -> int:
    strings = source_strings()
    if "--pot" in sys.argv:
        print(json.dumps({s: "" for s in sorted(strings)}, ensure_ascii=False, indent=2))
        return 0
    if "--fill" in sys.argv:
        code = sys.argv[sys.argv.index("--fill") + 1]
        path = CATALOGS / f"{code}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for s in strings:
            data.setdefault(s, "")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: now holds {len(data)} keys")
        return 0
    print(f"{len(strings)} translatable source strings\n")
    for cat in sorted(CATALOGS.glob("*.json")):
        data = json.loads(cat.read_text(encoding="utf-8"))
        have = {k for k, v in data.items() if v}
        missing = len(strings - have)
        stale = len(set(data) - strings)
        print(
            f"  {cat.stem}: {len(have & strings):4d}/{len(strings)} translated, "
            f"{missing} missing, {stale} stale"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
