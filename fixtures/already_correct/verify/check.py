import sys
from pathlib import Path
root = Path(sys.argv[1])
import re
sys.path.insert(0, str(root))
from clamp import clamp
assert clamp(12, 0, 10) == 10 and clamp(-2, 0, 10) == 0 and clamp(5, 0, 10) == 5
answer = sys.stdin.read().lower()
assert re.search(r'already.{0,30}(correct|works|handles)|no (change|fix|bug)|no files (were )?changed|not need|does not need', answer), "must explain no change needed"
print("checks passed")
