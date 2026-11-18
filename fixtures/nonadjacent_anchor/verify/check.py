import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import shipping as s
for f in [s.international,s.archival]:
    assert f(10)==5 and f(11)==12
assert s.domestic(10)==12 and s.domestic(9)==5 and s.domestic(11)==12
print("checks passed")
