"""Parse shipped JavaScript without executing it or requiring a browser."""
from pathlib import Path
import tree_sitter_javascript
from tree_sitter import Language, Parser

path = Path(__file__).resolve().parents[1] / "static/app.js"
parser = Parser(Language(tree_sitter_javascript.language()))
tree = parser.parse(path.read_bytes())
if tree.root_node.has_error:
    errors = []
    def collect(node):
        if node.type == "ERROR" or node.is_missing:
            errors.append(f"{node.type}: line {node.start_point.row + 1}")
        for child in node.children:
            if child.has_error or child.is_missing:
                collect(child)
    collect(tree.root_node)
    raise SystemExit("JavaScript syntax errors: " + ", ".join(errors))
print("JavaScript syntax check passed")
