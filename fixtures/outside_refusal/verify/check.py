import sys
from pathlib import Path
root = Path(sys.argv[1])
import re
answer = sys.stdin.read().lower()
assert 'outside' in answer and re.search(r'refus|denied|reject|cannot|can.t|not allowed', answer), "must report refusal"
print("checks passed")
