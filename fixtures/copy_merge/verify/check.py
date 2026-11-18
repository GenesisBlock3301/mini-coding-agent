import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from merge import merged
a={"x":1,"nested":[]}
b={"x":2,"y":0}
r=merged(a,b)
assert r=={"x":2,"nested":[],"y":0}
assert a=={"x":1,"nested":[]} and b=={"x":2,"y":0}
assert r is not a and r is not b
assert merged({}, {})=={}
print("checks passed")
