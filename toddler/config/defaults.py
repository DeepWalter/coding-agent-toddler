"""Default constants for Toddler — all tunable via env vars or config file."""

from pathlib import Path

# --- LLM Provider ---
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_CONTEXT_LENGTH = 128_000

# Thinking-effort tiers accepted from the environment and the CLI.  "none"
# disables thinking outright; the rest are handed to the provider, which
# collapses the finer ones onto DeepSeek's coarser low/high/max scale.
# The CLI validates its flag against this list so a typo fails at the
# parser — a bad tier reaching the provider is silently coerced, which is
# a costly way to find out.
REASONING_EFFORT_TIERS = [
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
]

# --- Agent Loop ---
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_MAX_TOKENS_PER_RESPONSE = 8192
DEFAULT_TEMPERATURE = 0.0

# --- Context Management ---
DEFAULT_COMPACTION_THRESHOLD = 0.8  # 80% of context window triggers compaction

# --- Streaming ---
STREAMING_ENABLED = True
MAX_OUTPUT_LINES = 40
MAX_OUTPUT_PANEL_HEIGHT = 0  # fallback when terminal size is unknown

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
