import sys
from pathlib import Path

# Insert script/ into sys.path so all tests can import shared.* etc.
_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
