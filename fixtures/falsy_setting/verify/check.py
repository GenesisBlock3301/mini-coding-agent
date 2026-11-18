import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from config import get_setting, display_setting
for value in [None,False,0,"",[],"x"]:
    assert get_setting({"k":value},"k","fallback")==value
assert get_setting({},"k","fallback")=="fallback"
assert display_setting({"k":0},"k","fallback")=="fallback"
print("checks passed")
