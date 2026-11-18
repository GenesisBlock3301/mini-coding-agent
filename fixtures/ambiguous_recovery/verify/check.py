import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import fees
assert fees.domestic_fee(9) == 5 and fees.domestic_fee(10) == 15
assert fees.export_fee(10) == 10 and fees.export_fee(11) == 30
print("checks passed")
