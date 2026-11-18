import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from api import page
rows=list(range(7))
assert page(rows,2,2)=={"items":[2,3],"total":7}
assert page(rows,0,0)=={"items":[],"total":7}
assert page(rows,20,5)=={"items":[],"total":7}
assert rows==list(range(7))
print("checks passed")
