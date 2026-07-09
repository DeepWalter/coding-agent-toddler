"""Default constants for Toddler — all tunable via env vars or config file."""

from pathlib import Path

# --- LLM Provider ---
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CONTEXT_WINDOW = 128_000

# --- Agent Loop ---
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_MAX_TOKENS_PER_RESPONSE = 8192
DEFAULT_TEMPERATURE = 0.0

# --- Context Management ---
DEFAULT_COMPACTION_THRESHOLD = 0.8  # 80% of context window triggers compaction

# --- Permissions ---
AUTO_APPROVE_READ = True
CONFIRM_WRITE = True
CONFIRM_SHELL_DANGEROUS = True

# --- Streaming ---
STREAMING_ENABLED = True

# --- Sessions & Data ---
SESSION_DIR = Path.home() / ".toddler"
SESSION_DB_NAME = "sessions.db"
MEMORY_FILE_NAME = "memory.json"
CHECKPOINT_BASE_DIR = "checkpoints"

# --- Checkpoints ---
CHECKPOINT_KEEP_LATEST = 50

# --- Plan Mode ---
PLAN_MODE_COMPLEXITY_KEYWORDS = [
    "refactor", "implement", "redesign", "restructure",
    "migrate", "overhaul", "rewrite", "rearchitect",
]
PLAN_MODE_MIN_WORDS = 200
PLAN_MODE_MULTI_FILE_INDICATORS = ["across", "multiple files", "and also"]

# --- Shell ---
SHELL_DEFAULT_TIMEOUT = 60  # seconds
SHELL_MAX_TIMEOUT = 300
