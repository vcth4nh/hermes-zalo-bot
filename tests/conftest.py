"""Test bootstrap.

Puts the repo root on sys.path so ``import zalo`` works, and adds the Hermes checkout named by
``HERMES_AGENT_SRC`` so the adapter tests can import ``gateway.*``. Without that variable (or an
editable Hermes install in the interpreter) the adapter tests skip themselves.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_hermes_src = os.environ.get("HERMES_AGENT_SRC", "").strip()
if _hermes_src and _hermes_src not in sys.path:
    sys.path.insert(0, _hermes_src)

# Hermes writes runtime status files under HERMES_HOME; keep tests out of the real ~/.hermes.
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="hermes-home-test-"))
