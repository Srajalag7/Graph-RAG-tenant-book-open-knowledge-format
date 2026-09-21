"""Configuration loader for the Ask the Tenant Book service.

Reads settings from a .env file in the project root and from environment variables.
Environment variables always take precedence over .env values.
"""

import os
import math
from pathlib import Path


def load_dotenv():
    """Load variables from .env file into os.environ (will NOT override existing env vars)."""
    # Search for .env: first in cwd, then in project root (parent of service/)
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    env_path = None
    for p in candidates:
        if p.exists():
            env_path = p
            break

    if env_path is None:
        return

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # setdefault: env vars set externally always win
            os.environ.setdefault(key, value)


# Auto-load .env on first import
load_dotenv()


# ── Exported settings ────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("MODEL", "gemini-3.5-flash-lite")
CORPUS_DIR = os.environ.get("CORPUS_DIR", "corpus")
BASE_URL = os.environ.get(
    "BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
PORT = int(os.environ.get("PORT", "8000"))
MAX_TRAVERSAL_ROUNDS = int(os.environ.get("MAX_TRAVERSAL_ROUNDS", "8"))
QUERY_TIMEOUT = float(os.environ.get("QUERY_TIMEOUT", "180"))

GEMINI_CALL_DELAY_SECONDS = float(os.environ.get("GEMINI_CALL_DELAY_SECONDS", "1"))
if not math.isfinite(GEMINI_CALL_DELAY_SECONDS) or GEMINI_CALL_DELAY_SECONDS < 0:
    raise ValueError("GEMINI_CALL_DELAY_SECONDS must be a finite, nonnegative number")
