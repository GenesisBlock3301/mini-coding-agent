import sys
from pathlib import Path
root = Path(sys.argv[1])
import re
answer = sys.stdin.read().lower()
assert re.search(r"(?<![\w])pricing[.]py(?![\w])", answer), "missing file"
assert re.search(r"(?<![\w])discounted_total(?![\w])", answer), "missing function"
print("checks passed")
