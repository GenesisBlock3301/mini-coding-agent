import sys
from pathlib import Path
root = Path(sys.argv[1])
import ast
sys.path.insert(0, str(root))
import settings, test_settings
assert settings.PAGE_SIZE == 25, "constant not updated"
tree = ast.parse((root / "test_settings.py").read_text())
assert any(isinstance(n, ast.Assert) and isinstance(n.test, ast.Compare)
           and isinstance(n.test.left, ast.Name) and n.test.left.id == 'PAGE_SIZE'
           and len(n.test.ops) == 1 and isinstance(n.test.ops[0], ast.Eq)
           and len(n.test.comparators) == 1 and isinstance(n.test.comparators[0], ast.Constant)
           and n.test.comparators[0].value == 25 for n in ast.walk(tree)), "assertion missing"
test_settings.test_default()
print("checks passed")
