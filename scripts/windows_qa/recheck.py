"""Run selected standalone case blocks without repeating completed transfers."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument(
    "script",
    choices=[
        "browser_checks",
        "browser_extended",
        "browser_collections",
        "browser_native_flows",
        "data_queue_checks",
        "browser_final_actions",
        "browser_edge_checks",
    ],
)
parser.add_argument("cases", nargs="+")
parser.add_argument("--url-tag", help="Fresh disposable query tag for a progress recheck")
args = parser.parse_args()
path = Path(__file__).with_name(args.script + ".py")
source = path.read_text(encoding="utf-8")
if args.url_tag:
    assert args.url_tag.replace("-", "").isalnum()
    source = source.replace("progress-real", args.url_tag).replace("pause-final", args.url_tag)
tree = ast.parse(source)
selected = []
found = set()
if args.script in ("data_queue_checks", "browser_final_actions", "browser_edge_checks"):

    class Cases(ast.NodeTransformer):
        def visit_With(self, node):
            expression = node.items[0].context_expr
            if (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "case"
            ):
                identifier = expression.args[0].value
                if identifier in args.cases:
                    found.add(identifier)
                    return node
                return ast.copy_location(ast.Pass(), node)
            return self.generic_visit(node)

    tree = Cases().visit(tree)
    assert found == set(args.cases)
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"))
    raise SystemExit(0)
for node in tree.body:
    if isinstance(node, ast.With):
        expression = node.items[0].context_expr
        identifier = expression.args[0].value
        if identifier in args.cases:
            found.add(identifier)
            selected.append(node)
    elif not isinstance(node, ast.For):
        selected.append(node)
assert found == set(args.cases), "Only standalone case blocks are supported"
tree.body = selected
exec(compile(tree, str(path), "exec"))
