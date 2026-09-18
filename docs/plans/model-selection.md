# Model Tiers, the `[1m]` Context Notation & New Defaults

## Motivation

Toddler's model is a single env-derived string (`DEEPSEEK_MODEL`, default
`deepseek-v4-pro`) with a hardcoded 128K context window
(`DEFAULT_MAX_CONTEXT_LENGTH`) and no way to say that the same model exists in a
long-context variant.  Three forces make that insufficient:

1. **Model choice should be a name, not a string.**  The user picks between a
   fast/cheap model and a stronger one; today that means editing an env var with
   a vendor-specific id and remembering it.

2. **The context window is a property of the model, not of the install.**  A
   `[1m]` variant and a plain model differ by 5x in window size, but the window
   is currently a global constant that cannot follow the model.

3. **The shipped defaults no longer match how the models are used.**  A
   thinking-mode run spends `max_tokens` on reasoning *and* output, so the 8192
   default truncates (`finish_reason="length"`, empty `content`); and the
   endpoint's own effort default is below what these models can do.

Two related defects surfaced during exploration and are handled here:

- **The window manager's output headroom is stale.**  `_DEFAULT_OUTPUT_HEADROOM`
  is 4096 (`toddler/context/window.py`), independent of the request's actual
  output budget.  Today's 8192 default already made that incoherent; raising it
  to 32000 makes the gap 8x.  The headroom should follow the configured budget.
- **`TODDLER_REASONING_EFFORT` cannot be tested.**  It is a plain class-body
  assignment in `Settings`, so `_env(...)` runs once at import; every other
  field re-reads env per instantiation.

**Scope.**  Config layer only — resolution happens once at `Settings`
construction.  Per-session switching (a `SessionManager` that owns model/effort
and threads it per turn into the provider, a `/model` command, live UI updates)
is a deliberate follow-up, not part of this change.

## Key Design Decisions

- **Three tiers — `default`, `pro`, `flash`.**  Each is a *slot* holding a model
  string, all shipping as `"deepseek-flash"` so a fresh install works with one
  model and no configuration.  Retargeting a slot later is an env var away.
  Tier `default` is what is used when the user selects nothing.
- **A tier name resolves through the slot table; anything else is a literal model
  id**, passed through verbatim.  `--model` therefore stays free-form (no
  `choices=`), because literal ids must keep working.
- **`[1m]` is Toddler-local notation**, not part of the model id: it is stripped
  before the wire request.  `"deepseek-flash[1m]"` sends `model="deepseek-flash"`
  and is accounted for as a 1M-token window locally.
- **No escape hatch for "let the endpoint pick its own effort".**  Effort is
  always sent, defaulting to `"max"`.  `"none"` still exists for disabling
  thinking outright; the endpoint's implicit default is no longer reachable.

### Precedence

| Concern | Order |
|---|---|
| selection | `--model` > `TODDLER_MODEL` > `DEEPSEEK_MODEL` > `"default"` |
| slot value | `TODDLER_MODEL_<TIER>` > `DEFAULT_MODEL` (`"deepseek-flash"`) |
| window | `TODDLER_MAX_CONTEXT_LENGTH` > `[1m]` suffix > `DEFAULT_MAX_CONTEXT_LENGTH` (200 000) |
| effort | `--reasoning-effort` > `TODDLER_REASONING_EFFORT` > `"max"` |
| output budget | `TODDLER_MAX_TOKENS` > 32 000 |

`DEEPSEEK_MODEL` is honored as a **selection** fallback.  Today it *is* the model
string and is typically set in `~/.toddler/.env`; its common value is a literal id
that resolves to itself, so existing installs behave identically on upgrade
instead of silently flipping to `deepseek-flash`.

### The context window is a property, not a field

`Settings.from_cli` overlays CLI args by merging resolved field values:

```python
base = cls()
return cls(**{**base.__dict__, **cli_overrides})
```

`base.__dict__` carries every field *already resolved* against the env/default
model.  So a value derived from `model` must not be a stored field:

- a stored derived value would ride through the merge and **win over the
  CLI-overridden model** — the stale-window bug;
- a non-init field added to `__dict__` would make `cls(**...)` raise `TypeError`
  outright.

Making it a read-only `@property` fixes both *by construction*: a value that is
never in `__dict__` cannot go stale through the merge.  It is also the smallest
diff — `from_cli`, `_cli_fields()`, and the single consumer in `ContextManager`
all stay unchanged.

```python
model: str = field(
    default_factory=lambda: (
        _env("TODDLER_MODEL") or _env("DEEPSEEK_MODEL") or defaults.DEFAULT_TIER
    )
)
max_context_length_override: int | None = field(
    default_factory=lambda: _env_int_opt("TODDLER_MAX_CONTEXT_LENGTH")
)

def __post_init__(self) -> None:
    # Resolve a tier name to the concrete model spec, in place.  Idempotent —
    # a spec is never itself a tier name — so the merge's second pass is a
    # no-op and `self.model` is the resolved spec everywhere downstream.
    self.model = models.resolve_model(self.model)

@property
def max_context_length(self) -> int:
    """Context window in tokens — explicit override, else derived from the
    model spec's ``[1m]`` suffix."""
    if self.max_context_length_override is not None:
        return self.max_context_length_override
    return models.context_length_for(self.model)
```

The sentinel is `None` — matching `reasoning_effort: str | None` and every CLI
arg's `default=None` — which needs one new helper beside `_env_int`:

```python
def _env_int_opt(key: str) -> int | None:
    val = _env(key)
    return int(val) if val is not None else None
```

### Identity vs. wire

`Settings.model` and `provider.model` hold the **spec** (`"deepseek-flash[1m]"`);
only the request kwargs carry the stripped id.  This keeps the CLI banner,
`/api/meta`, and the WS `session_info` frames showing the same string as the
`conversation.model` cache key — one identity, visible everywhere, rewritten in
exactly one place (the provider's wire call sites).

`TokenCounter` / `ContextWindowManager` also receive the spec: the encoding
lookup is a lowercase `startswith` prefix match, so `"deepseek-flash[1m]"` still
matches `deepseek`.  That is coincidental rather than designed, so it is pinned
by a test.

## Implementation

### `toddler/config/models.py` (new)

The notation's single owner — tier lookup, spec parsing, and the window table.
Imports only `os`, `re`, `logging`, and `toddler.config.defaults` (importing
`settings` from here would be a cycle).

- `TIER_ENV_PREFIX = "TODDLER_MODEL_"`, `_SUFFIX_LENGTHS = {"1m": 1_000_000}`,
  `_SUFFIX_RE = re.compile(r"\[([^\]]*)\]$")`.
- `tier_value(tier) -> str` — `os.getenv(f"TODDLER_MODEL_{tier.upper()}", DEFAULT_MODEL)`.
- `resolve_model(selection: str) -> str` — strip; a falsy selection becomes
  `DEFAULT_TIER` (so `--model ""` cannot send an empty id); a case-insensitive
  match against `MODEL_TIERS` resolves through `tier_value`, anything else is
  returned **verbatim** (never lowercased — literal ids are case-sensitive).
- `split_spec(spec) -> tuple[str, str | None]` — the single parser for the
  notation; `wire_model` and `context_length_for` both build on it.
- `wire_model(spec) -> str` — the model id as the endpoint expects it.
- `context_length_for(spec, *, default=DEFAULT_MAX_CONTEXT_LENGTH) -> int` — a
  known suffix (case-insensitive) yields its length; an unrecognized suffix warns
  and falls back to the default (stripping it beats a confusing 404, since a
  bracket is never legal in an OpenAI model id); no suffix yields the default.

The module docstring documents the notation, the 200K/1M rule, and that tier
lookup is **one level only** — a slot value that names another tier is used
verbatim.

### `toddler/config/defaults.py`

- `DEFAULT_MODEL = "deepseek-flash"` — the value a slot holds when its env var is
  unset (was `"deepseek-v4-pro"`).
- `MODEL_TIERS = ("default", "pro", "flash")`, `DEFAULT_TIER = "default"`.
- `DEFAULT_MAX_CONTEXT_LENGTH = 200_000` — the window assumed when the spec
  carries no suffix (was `128_000`).
- `DEFAULT_MAX_TOKENS_PER_RESPONSE = 32_000` (was `8192`).
- `DEFAULT_REASONING_EFFORT = "max"`, beside `REASONING_EFFORT_TIERS`.

### `toddler/config/settings.py`

- New `_env_int_opt` helper; the `model` factory and `max_context_length`
  property above.
- `reasoning_effort` becomes a `field(default_factory=...)` reading
  `TODDLER_REASONING_EFFORT` with `DEFAULT_REASONING_EFFORT` as the fallback, so
  the env is re-read per instantiation and tests can monkeypatch it.
- `__post_init__` resolves the model spec.
- `from_cli` and `_cli_fields()` are unchanged.

### `toddler/config/__init__.py`

Re-export `DEFAULT_REASONING_EFFORT`, `DEFAULT_TIER`, and `MODEL_TIERS` from both
the import block and `__all__` (the two lists must be edited together).  The
`models.py` functions are deliberately *not* re-exported; the provider imports
them by module path.

### `toddler/llm/provider.py`

The spec stays on `self._model` (identity, display, persistence, the `model`
property) and one stripped attribute feeds every wire site, so the `deepseek-`
dispatch in `_parse_params` and all four request paths pick it up together:

| Site | Change |
|---|---|
| `__init__` | add `self._wire_model = wire_model(self._model)` |
| `generate` | `model = self.model` → `model = self._wire_model` (feeds `_parse_params`) |
| `generate_compact` | both the `_parse_params` argument and the `model=` kwarg |
| `_generate_streaming` | `model=self._wire_model` |
| `_generate_non_streaming` | `model=self._wire_model` |

The module docstring's `max_tokens` note (`default 8192`) is updated to 32000.

Persistence stores the spec, so toggling `[1m]` on an existing conversation
invalidates its stored `total_tokens` baseline and costs one tiktoken
re-estimate on resume.  Conservative and harmless — the column keeps its meaning.

### `toddler/context/manager.py`

Pass the configured output budget as the window manager's headroom, so the
reserve follows the request instead of a stale constant:

```python
self._window_mgr = ContextWindowManager(
    llm_provider.model,
    max_context_length=settings.max_context_length,
    output_headroom=settings.max_tokens_per_response,
)
```

Without this, a 32K-output request can exceed the context window near the
compaction threshold.

## Verification

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check toddler/ tests/
```

End-to-end, with the relevant vars unset:

```bash
env -u DEEPSEEK_MODEL -u TODDLER_MODEL .venv/bin/tod "say hi"
#   -> banner + prompt header show  Model: deepseek-flash ; 200K window

TODDLER_MODEL_PRO='deepseek-v4-pro[1m]' .venv/bin/tod --model pro "say hi"
#   -> header shows deepseek-v4-pro[1m]; the request sends model="deepseek-v4-pro"
```

And with a conflicting env to prove precedence:
`TODDLER_MODEL=flash DEEPSEEK_MODEL=deepseek-v4-pro .venv/bin/python -m pytest tests/ -q`

## Accepted Risks

- **`"max"` is DeepSeek-shaped.**  The non-DeepSeek branch of `_parse_params`
  passes effort through untranslated, and `"max"` is not a valid effort for a
  real OpenAI endpoint.  Only bites if a slot is retargeted to `gpt-*`; the fix
  belongs in the provider layer (family-aware defaults).
- **`[1m]` makes compaction expensive.**  `should_compact` fires at 0.8 of the
  window, i.e. an ~800K-token summarization call against a 1024-token output
  budget.  Existing behavior, newly reachable; `DEFAULT_COMPACTION_THRESHOLD` is
  the lever if it hurts.
- **One-level tier resolution.**  `TODDLER_MODEL_PRO=default` uses the literal
  string `"default"` as a model id rather than aliasing the default tier.
