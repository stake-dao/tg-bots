import importlib
import os
from pathlib import Path
import socket
import sys

import dotenv
import pytest

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root / "script"))
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
dotenv.load_dotenv = lambda *args, **kwargs: False
for key in list(os.environ):
    if any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "REDIS", "WORKER")):
        os.environ.pop(key)
os.environ["PROD"] = "False"


def blocked(*args, **kwargs):
    raise AssertionError("Network access blocked during validation")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked

for path in sorted((root / "script").rglob("*.py")):
    module = ".".join(path.relative_to(root / "script").with_suffix("").parts)
    importlib.import_module(module)
print("All extracted modules imported with network blocked and credentials absent")
raise SystemExit(pytest.main(["-q", "tests"]))
