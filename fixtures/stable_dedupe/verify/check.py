import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from unique import dedupe
assert dedupe([3,1,3,2,1])==[3,1,2]
items=[[1], {"a":2}, [1], {"a":2}, []]
assert dedupe(items)==[[1],{"a":2},[]]
assert items==[[1], {"a":2}, [1], {"a":2}, []]
assert dedupe([])==[]
print("checks passed")
