import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ranges import overlaps
for a,b,want in [((1,3),(3,5),False),((1,4),(3,5),True),((2,2),(1,3),False),((1,3),(2,2),False),((1,5),(2,3),True)]:
    assert overlaps(a,b)==want
    assert overlaps(b,a)==want
for a,b in [((3,1),(0,5)),((0,5),(3,1))]:
    try: overlaps(a,b)
    except ValueError: pass
    else: raise AssertionError("reversed accepted")
print("checks passed")
