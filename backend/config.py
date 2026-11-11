import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("EXPLORER_DATA_DIR", PROJECT_ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "explorer.db"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

MAX_UPLOAD_BYTES = int(float(os.environ.get("EXPLORER_MAX_UPLOAD_MB", "50")) * 1024 * 1024)
MAX_COLUMNS = int(os.environ.get("EXPLORER_MAX_COLUMNS", "500"))
CACHE_SIZE = int(os.environ.get("EXPLORER_CACHE_SIZE", "8"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.environ.get("EXPLORER_AI_MODEL", "claude-haiku-5-5")
AI_TIMEOUT_SECONDS = float(os.environ.get("EXPLORER_AI_TIMEOUT", "30"))
