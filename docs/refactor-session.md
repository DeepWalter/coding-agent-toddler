# Refactor: CLI + Storage → Session Coordinator

## Target Architecture

```
                 CLI
                  |
                  v
          SessionCoordinator
                  |
       +----------+----------+
       |                     |
       v                     v
    AgentLoop          ConversationContext
       |                     |
       v                     v
    Tools              StorageManager
       |
       v
      LLM
```

- **CLI** imports ONLY from `session` (and `config` for Settings)
- **SessionCoordinator** imports from `agent`, `context`, `tools`, `session.manager`
- **AgentLoop** imports from `tools`, `llm` (already correct)
- **ConversationContext** imports from `session.manager` (already correct, via
  TYPE_CHECKING)
- **StorageManager** imports from `llm`, `session.models`, `session.store`

## Current State — Problems

**CLIApp is a god class** (~1000 lines). It directly creates and manages:
- ToolRegistry + ToolExecutor
- AgentLoop (lazily)
- ConversationContext + SystemPromptBuilder
- StorageManager (passed in, but used directly)
- CheckpointManager (wired after session resolution)
- SlashCommandDispatcher
- Session lifecycle (create/resume/save/prune)
- Conversation switching (/clear, /resume)
- Auto-titling
- Agent turn execution (streaming + non-streaming paths)

**Current import graph** (simplified):
```
main.py ──→ cli, agent, config, context, llm, storage, tools  (8 packages)
cli/app  ──→ agent, cli, config, context, llm, storage, tools, checkpoint  (8 pkgs)
agent    ──→ agent, llm, tools  (+ context via TYPE_CHECKING)
context  ──→ llm, context  (+ storage via TYPE_CHECKING)
storage  ──→ llm, storage
```

Every package fans in to every other. No layered separation.

## Step 1: Rename `storage/` → `session/`

**Goal**: Align package name with its future role as the session lifecycle owner.

**Actions**:
1. `git mv toddler/storage toddler/session`
2. Update all imports across the codebase:
   - `toddler/main.py` — `toddler.storage` → `toddler.session`
   - `toddler/cli/app.py` — `toddler.storage.manager` → `toddler.session.manager`,
     `toddler.storage.models` → `toddler.session.models`
   - `toddler/cli/commands.py` — same pattern
   - `toddler/context/conversation_context.py` — TYPE_CHECKING imports
   - `toddler/checkpoint/manager.py` — storage references
   - `toddler/session/__init__.py` — internal imports
   - `toddler/session/manager.py` — internal imports
   - All test files
3. Update `toddler/session/__init__.py` docstring
4. Run full test suite — must pass with zero changes

**No class renames.** `StorageManager`, `Session`, `Conversation`, etc. keep
their names. This is purely a package rename.

**Verification**: `git diff --stat` shows only import path changes. All tests
pass.

## Step 2: Create `SessionCoordinator` in `session/coordinator.py`

**Goal**: Extract orchestration logic from `CLIApp` into a new class that owns
the session lifecycle. `CLIApp` delegates all business logic to the coordinator
and only handles display + input.

**New file**: `toddler/session/coordinator.py`

### SessionCoordinator API

```python
class SessionCoordinator:
    """Owns the lifecycle of a session — wires Agent, Context, and Storage.

    The CLI talks ONLY to this object. It creates and manages:
    - ToolRegistry + ToolExecutor
    - ConversationContext + SystemPromptBuilder
    - AgentLoop (lazily)
    - CheckpointManager (deferred until session resolution)
    """

    def __init__(
        self,
        settings: Settings,
        storage_manager: StorageManager,
        llm: BaseLLMProvider,
        *,
        store: SQLiteStore | None = None,
        repo_root: Path | None = None,
        project_mapper: ProjectMapper | None = None,
        persistent_memory: PersistentMemory | None = None,
        context_window_mgr: ContextWindowManager | None = None,
        conversation_compactor: ConversationCompactor | None = None,
        state_machine: AgentStateMachine | None = None,
    ) -> None: ...

    # -- Session lifecycle --
    async def resolve(self, session_id: str | None = None) -> Session: ...
    @property
    def session(self) -> Session | None: ...

    # -- Turn execution (the core API the CLI calls) --
    async def process_turn(
        self, user_input: str, *, force_plan: bool = False,
    ) -> AsyncIterator[AgentEvent]: ...

    # -- Conversation management --
    async def new_conversation(self, title: str | None = None) -> None: ...
    async def resume_conversation(self, conversation_id: str) -> None: ...
    async def switch_session(self, session_id: str) -> None: ...

    # -- Persistence --
    async def save(self) -> None: ...
    async def prune_if_empty(self) -> None: ...

    # -- Accessors (for slash commands / info display) --
    @property
    def agent(self) -> AgentLoop: ...
    @property
    def context(self) -> ConversationContext: ...
```

### What moves from CLIApp → SessionCoordinator

| Responsibility | Current location (CLIApp) | Moves to SessionCoordinator |
|---|---|---|
| Tool registry + executor creation | `__init__` | `__init__` |
| SystemPromptBuilder creation | `__init__` | `__init__` |
| Session resolution (`get_or_create`) | `run_repl` / `run_one_shot` | `resolve()` |
| ConversationContext creation + activation | `run_repl` / `run_one_shot` | `resolve()` |
| Checkpoint wiring | `_wire_checkpointing` | `resolve()` (internal) |
| AgentLoop lazy creation (`_agent` property) | `_agent` property | `agent` property |
| `_run_agent_turn` | CLIApp | `process_turn()` |
| `_run_streaming_turn` | CLIApp | `process_turn()` (internal) |
| `_process_events` (non-streaming) | CLIApp | `process_turn()` (internal) |
| `_auto_title` / `_auto_title_background` | CLIApp | private methods |
| `_clear_conversation` | CLIApp | `new_conversation()` |
| `_resume_conversation` | CLIApp | `resume_conversation()` |
| `_switch_session` | CLIApp | `switch_session()` |
| `_prune_empty_session` | CLIApp | `prune_if_empty()` |

### What stays in CLIApp

| Responsibility | Why |
|---|---|
| REPL loop (`while True: prompt()`) | Pure I/O concern |
| Rendering events (TextDelta, ToolCallStart, etc.) | Pure display concern |
| StreamDisplay lifecycle | Pure display concern |
| Confirmation prompts (AgentPaused) | Requires user input |
| Slash command dispatch | But dispatch targets call SessionCoordinator, not StorageManager directly |
| Banner, help text | Pure display concern |

## Step 3: Slim down `CLIApp`

**Goal**: CLIApp becomes a thin display+input layer that delegates to
SessionCoordinator.

### After refactoring — CLIApp

```python
class CLIApp:
    """Thin CLI layer — REPL loop, display, input, slash commands."""

    def __init__(
        self, settings: Settings, session: SessionCoordinator,
    ) -> None:
        self._settings = settings
        self._session = session
        self._renderer = Renderer()
        self._input = InputHandler()
        self._cmd_dispatcher = SlashCommandDispatcher(
            state_machine=session.state_machine,
            session_coordinator=session,  # was storage_manager
        )

    async def run_repl(self, *, session_id: str | None = None) -> None:
        await self._session.resolve(session_id)
        self._print_banner()
        while True:
            user_input = await self._input.prompt(...)
            if user_input.startswith("/"):
                handled = await self._handle_slash_command(user_input)
                if not handled:
                    break
                continue
            # Delegate turn to coordinator, just render events
            async for event in self._session.process_turn(user_input):
                self._render_event(event)
        await self._session.prune_if_empty()

    async def run_one_shot(self, query, *, force_plan, session_id) -> None:
        await self._session.resolve(session_id)
        async for event in self._session.process_turn(query, force_plan=force_plan):
            self._render_event(event)
        await self._session.save()
```

### CLIApp imports after refactoring

Before: 8 packages (agent, cli, config, context, llm, storage, tools, checkpoint)
After: 3 packages (session, cli, config)

Specific removed imports:
- `toddler.agent.events` — events still used for rendering, keep
- `toddler.agent.loop` — **removed** (AgentLoop created by coordinator)
- `toddler.agent.state_machine` — **removed** (owned by coordinator)
- `toddler.checkpoint` — **removed** (wired by coordinator)
- `toddler.context.conversation_context` — **removed**
- `toddler.context.system_prompt` — **removed**
- `toddler.llm.base` / `toddler.llm.provider` — **removed**
- `toddler.session.manager` — **removed** (StorageManager accessed via coordinator)
- `toddler.session.models` — keep `Session` for display (or access via
  `coordinator.session`)
- `toddler.tools` / `toddler.tools.executor` — **removed**

## Step 4: Simplify `main.py`

**Goal**: Reduce main.py's import surface. Wire SessionCoordinator once, pass
to CLIApp.

### After refactoring — main.py

```python
def main() -> None:
    settings = Settings.from_cli(args)
    setup_logging(verbose=args.verbose, log_dir=settings.session_dir)

    # --- Session persistence ---
    db_path = settings.session_dir / "sessions.db"
    store = SQLiteStore(db_path)
    store.open()
    storage_mgr = StorageManager(store)

    if args.list_sessions:
        asyncio.run(print_sessions(storage_mgr))
        return

    # --- Shared services ---
    llm = OpenAICompatibleProvider(settings)

    # --- Session coordinator (owns all wiring) ---
    session = SessionCoordinator(
        settings,
        storage_mgr,
        llm,
        store=store,
        repo_root=Path.cwd(),
        project_mapper=ProjectMapper(),
        persistent_memory=PersistentMemory(settings.session_dir),
        context_window_mgr=ContextWindowManager(llm),
        conversation_compactor=ConversationCompactor(llm),
        state_machine=AgentStateMachine(),
    )

    # --- CLI (thin display + input layer) ---
    app = CLIApp(settings, session)

    query = " ".join(args.query).strip() if args.query else ""
    if query:
        asyncio.run(app.run_one_shot(query, force_plan=args.plan,
                                      session_id=args.session))
    else:
        asyncio.run(app.run_repl(session_id=args.session))
```

### Import count reduction

Before: 8 direct package imports
After: 4 direct package imports (`session`, `cli`, `config`, `llm`)

## Step 5: Update `SlashCommandDispatcher`

**Goal**: Commands that mutate session state go through `SessionCoordinator`
instead of calling `StorageManager` directly.

Current sentinel-string protocol (e.g. `"__NEW_CONVERSATION__:title"`) is
replaced with direct calls on the coordinator:

```python
class SlashCommandDispatcher:
    def __init__(
        self,
        state_machine: AgentStateMachine,
        session_coordinator: SessionCoordinator,  # was storage_manager
        checkpoint_manager_provider: ... = None,
    ): ...
```

Sentinel strings that become direct coordinator calls:
| Sentinel | Replacement |
|---|---|
| `__SESSION_INFO__` | CLI reads `coordinator.session` directly |
| `__LIST_CONVERSATIONS__` | CLI calls `coordinator.list_conversations()` |
| `__SESSION_SWITCH__:id` | CLI calls `coordinator.switch_session(id)` |
| `__NEW_CONVERSATION__:title` | CLI calls `coordinator.new_conversation(title)` |
| `__RESUME_CONVERSATION__:id` | CLI calls `coordinator.resume_conversation(id)` |

## Files Changed Summary

| File | Change |
|---|---|
| `toddler/storage/` → `toddler/session/` | Directory rename |
| `toddler/session/__init__.py` | Update exports, add `SessionCoordinator` |
| `toddler/session/coordinator.py` | **NEW** — SessionCoordinator class |
| `toddler/session/manager.py` | Update internal import paths |
| `toddler/cli/app.py` | Major deletion — remove orchestration, keep display+I/O |
| `toddler/cli/commands.py` | Replace `StorageManager` param with `SessionCoordinator` |
| `toddler/main.py` | Simplify wiring — create SessionCoordinator, pass to CLIApp |
| `toddler/context/conversation_context.py` | Update import paths (`storage` → `session`) |
| `toddler/checkpoint/manager.py` | Update import paths |
| Tests | Update imports, add SessionCoordinator tests |

## What Does NOT Change

- `AgentLoop` — already receives `ConversationContext`, no changes needed
- `ConversationContext` — already depends on `StorageManager`, no changes needed
- `Tools` package — no changes
- `LLM` package — no changes
- `Config` package — no changes
- `SQLiteStore` — no changes
- Data models (`Session`, `Conversation`, `StoredMessage`) — no renames

## Migration Strategy

Each step is a standalone, testable commit:

| # | Commit | Scope | Risk |
|---|--------|-------|------|
| 1 | `refactor(session): rename storage package to session` | Directory rename + import updates. Zero behavior change. | Low |
| 2 | `refactor(session): extract SessionCoordinator from CLIApp` | New file. Move turn execution + lifecycle logic. CLIApp delegates. Update tests. | Medium |
| 3 | `refactor(cli): slim CLIApp to display+input only` | Remove direct agent/context/tools imports from CLI. Route slash commands through coordinator. | Medium |
| 4 | `refactor(main): simplify wiring via SessionCoordinator` | Reduce main.py imports. Update SlashCommandDispatcher. | Low |

### Rollback

Each commit is independently revertible. If Step 2 introduces issues, revert to
Step 1 (package rename only, no behavioral change). If Step 3 breaks display
behavior, revert to Step 2 (coordinator exists but CLIApp still has old code
paths).

## Decisions

1. **Agent → LLM dependency**: Deferred. Changing how AgentLoop calls the LLM
   is a separate concern from the CLI → Session boundary refactor.

2. **SlashCommandDispatcher protocol**: Switch to direct method calls on
   `SessionCoordinator`. The sentinel-string protocol was a workaround for
   CLIApp not having a proper coordinator to delegate to. With
   `SessionCoordinator` in place, direct calls are cleaner.
