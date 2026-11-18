import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import names, page
assert hasattr(names, 'slugify') and not hasattr(names, 'slug'), "rename missing"
assert page.page_path("Hello World") == "/hello-world"
assert "slugify" in (root / "page.py").read_text(), "caller not updated"
print("checks passed")
