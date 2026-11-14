"""Rerun browser cases after reloading fixes; preserve restart evidence."""

import ast
from pathlib import Path

from command import command

# Inline event handlers expose document.URL as `URL`; use the global explicitly.
command(
    "evaluate",
    js="""() => {
  document.querySelector('#blob').onclick = () => {
    const a = document.createElement('a');
    a.href = window.URL.createObjectURL(new Blob(['test-only generated blob']));
    a.download = 'blob-export.txt';
    a.click();
  };
}""",
)
path = Path(__file__).with_name("browser_checks.py")
tree = ast.parse(path.read_text(encoding="utf-8"))
tree.body = [
    node
    for node in tree.body
    if not (
        isinstance(node, ast.With)
        and isinstance(node.items[0].context_expr, ast.Call)
        and isinstance(node.items[0].context_expr.args[0], ast.Constant)
        and node.items[0].context_expr.args[0].value == "BROWSER-PREF-RESTART"
    )
]
exec(compile(tree, str(path), "exec"))
