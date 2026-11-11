import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# Isolated data dir and no AI key for every test run.
os.environ["EXPLORER_DATA_DIR"] = tempfile.mkdtemp(prefix="explorer-test-")
os.environ.pop("ANTHROPIC_API_KEY", None)
