import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bank import transfer
for amount in [0,-1,11]:
    a={"a":10,"b":2}
    try: transfer(a,"a","b",amount)
    except ValueError: pass
    else: raise AssertionError("accepted invalid transfer")
    assert a=={"a":10,"b":2}
a={"a":10,"b":2}
transfer(a,"a","b",10)
assert a=={"a":0,"b":12}
a={"a":10}
transfer(a,"a","a",4)
assert a=={"a":10}
print("checks passed")
