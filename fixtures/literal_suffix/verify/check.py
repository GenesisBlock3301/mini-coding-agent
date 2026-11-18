import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from paths import remove_suffix as f
for name,suffix,want in [("report.txt",".txt","report"),("text",".txt","text"),("a.tar.tar",".tar","a.tar"),("abc","","abc"),("abc","abc",""),("x.TXT",".txt","x.TXT")]:
    assert f(name,suffix)==want
print("checks passed")
