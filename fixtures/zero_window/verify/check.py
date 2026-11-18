import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from window import recent
for n, want in [(0, []),(-1, []),(1,[3]),(3,[1,2,3]),(9,[1,2,3])]:
    source=[1,2,3]
    assert recent(source,n)==want
    assert source==[1,2,3]
assert recent([],0)==[]
print("checks passed")
