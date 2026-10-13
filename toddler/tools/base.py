"""Base tool abstractions — permissions, file scope, ToolResult, BaseTool."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Permission(Enum):
    """Permission tier for a tool.

    Used by the ``ToolExecutor`` to decide whether auto-approval is allowed
    or user confirmation is needed.
    """

    READ = "read"               # side-effect-free reads (auto-approve)
    WRITE = "write"             # file mutations (confirm by default)
    SHELL_SAFE = "shell_safe"   # safe shell commands — ls, git status, etc.
    SHELL_DANGEROUS = "shell_dangerous"  # rm, sudo, curl, etc. (always confirm)  # noqa: E501


class FileScope(Enum):
    """Where the files of one tool call live.

    A tier says what a call does; a scope says how far it reaches.  The
    two together decide the gate, so a ``read_file`` of ``~/.ssh/id_rsa``
    is not the same request as a ``read_file`` of a module in the
    workspace, though both are :attr:`Permission.READ`.
    """

    WORKSPACE = "workspace"     # inside the workspace — the agent's own ground
    EXTERNAL = "external"       # outside it — the user's call, never automatic
    RESTRICTED = "restricted"   # on the restricted list — never, at any tier


class PermissionMode(Enum):
    """Permission gating policy for tool execution.

    ``MANUAL`` (the default) auto-approves READ + SHELL_SAFE and
    requires confirmation for WRITE + SHELL_DANGEROUS.  ``AUTO``
    additionally auto-approves WRITE, leaving only SHELL_DANGEROUS
    to confirm.  Both apply to workspace files alone — a call that
    reaches outside asks, whatever the mode.
    """

    MANUAL = "manual"
    AUTO = "auto"


class Decision(Enum):
    """What the gate does with one tool call.

    ``DENY`` is the one the user cannot overrule from the approval
    prompt: it is policy, not a question.
    """

    ALLOW = "allow"   # run it
    ASK = "ask"       # pause and let the user decide
    DENY = "deny"     # refused outright, whatever the mode


# ---------------------------------------------------------------------------
# File scope
# ---------------------------------------------------------------------------


def _expand_path(
    value: str | os.PathLike[str] | None,
    *,
    base: Path | None = None,
) -> Path | None:
    """*value* as an absolute path with ``~`` expanded, or ``None``.

    A relative value is joined to *base* when one is given, and left to
    the process working directory otherwise (what the tools themselves
    do).  Unusable input answers ``None`` rather than raising: a
    classifier runs on model-authored arguments before the call is
    dispatched, and an empty string, a NUL or a ``~nosuchuser`` must not
    take the run down.  ``None`` is not a hole — the caller reads it as
    outside every scope.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        path = Path(value).expanduser()
    except (RuntimeError, ValueError):
        return None
    if base is not None and not path.is_absolute():
        path = base / path
    return path


def _resolve_path(
    value: str | os.PathLike[str] | None,
    *,
    base: Path | None = None,
) -> Path | None:
    """What a call naming *value* would open — symlinks followed."""
    path = _expand_path(value, base=base)
    if path is None:
        return None
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _normalize_path(
    value: str | os.PathLike[str] | None,
    *,
    base: Path | None = None,
) -> Path | None:
    """How *value* was written — no symlink followed.

    :func:`_resolve_path` answers what a call would open; this answers
    what it names.  Both are asked about the restricted list: a path
    listed as restricted stays restricted once it has been swapped for a
    symlink pointing elsewhere, which the resolved reading alone would
    report as merely external.
    """
    path = _expand_path(value, base=base)
    if path is None:
        return None
    return Path(os.path.normpath(path))


class FileScopePolicy:
    """Places a path — workspace, external, or restricted.

    One instance per session, built by
    :class:`~toddler.session.manager.SessionManager` from the working
    directory and the configured restricted list, and handed to every
    tool as it is constructed (see
    :func:`~toddler.tools.create_default_registry`) so a tool can answer
    for its own call.

    The workspace root and every restricted entry are expanded and
    resolved once, here, so all later comparisons are resolved against
    resolved: a root reached through a symlink (``/tmp`` on macOS) and a
    path spelled through the same link then compare equal.

    Parameters
    ----------
    workspace_root:
        The directory the agent works in.  It and everything beneath it
        are :attr:`FileScope.WORKSPACE`.
    restricted:
        Paths the agent may never touch — a file, or a directory and its
        whole subtree.  A relative entry names a path inside the
        workspace; ``~`` expands to home.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        restricted: Iterable[str | os.PathLike[str]] = (),
    ) -> None:
        self._root = Path(workspace_root).expanduser().resolve()
        entries: list[Path] = []
        for entry in restricted:
            resolved = _resolve_path(entry, base=self._root)
            if resolved is not None:
                entries.append(resolved)
        self._restricted = tuple(entries)

    @classmethod
    def default(cls) -> FileScopePolicy:
        """A policy for the process working directory and the configured
        restricted paths.

        What a tool answers with when it was handed no session policy —
        a bare ``Shell()`` in a test, the definition-time catalog.  The
        config is imported here rather than at module level: importing
        ``toddler.config`` builds the settings singleton (and loads
        ``~/.toddler/.env``), and ``tools.base`` is imported by nearly
        everything.
        """
        from toddler.config import defaults

        return cls(Path.cwd(), defaults.RESTRICTED_PATHS)

    @property
    def workspace_root(self) -> Path:
        """The directory the agent works in."""
        return self._root

    @property
    def restricted(self) -> tuple[Path, ...]:
        """The restricted entries, resolved."""
        return self._restricted

    def classify(self, path: str | os.PathLike[str] | None) -> FileScope:
        """The scope *path* lives in.

        ``RESTRICTED`` when it is a restricted entry or beneath one,
        ``WORKSPACE`` when it is the root or beneath it, ``EXTERNAL``
        otherwise — including when the path cannot be resolved at all,
        which asks rather than allows.
        """
        resolved = _resolve_path(path, base=self._root)
        if resolved is None:
            return FileScope.EXTERNAL
        if self._is_restricted(resolved):
            return FileScope.RESTRICTED
        # A restricted entry that is now a link out of the tree is still
        # the path the user listed, so the lexical reading is asked too.
        lexical = _normalize_path(path, base=self._root)
        if lexical is not None and self._is_restricted(lexical):
            return FileScope.RESTRICTED
        if resolved == self._root or self._root in resolved.parents:
            return FileScope.WORKSPACE
        return FileScope.EXTERNAL

    def classify_many(
        self, paths: Iterable[str | os.PathLike[str] | None]
    ) -> FileScope:
        """The strictest scope among *paths*.

        Restricted beats external beats workspace: one restricted file
        in the list makes the whole call restricted, because the call
        would touch it.  No paths at all is :attr:`FileScope.WORKSPACE`
        — a call that names no file adds no file risk, and its
        permission tier alone then decides.
        """
        scope = FileScope.WORKSPACE
        for path in paths:
            found = self.classify(path)
            if found is FileScope.RESTRICTED:
                return FileScope.RESTRICTED
            if found is FileScope.EXTERNAL:
                scope = FileScope.EXTERNAL
        return scope

    def restricted_match(
        self, paths: Iterable[str | os.PathLike[str] | None]
    ) -> Path | None:
        """The first path in *paths* that lands on a restricted entry.

        For the denial message, so a refusal can name what it refused
        rather than leaving the model to guess which argument was the
        problem.
        """
        for path in paths:
            if self.classify(path) is FileScope.RESTRICTED:
                resolved = _resolve_path(path, base=self._root)
                return resolved if resolved is not None else Path(str(path))
        return None

    def _is_restricted(self, path: Path) -> bool:
        """Whether *path* is a restricted entry or beneath one."""
        return any(
            path == entry or entry in path.parents
            for entry in self._restricted
        )


# ---------------------------------------------------------------------------
# Permission manager
# ---------------------------------------------------------------------------


class PermissionManager:
    """Mutable container for the current :class:`PermissionMode`.

    Owned by :class:`~toddler.session.manager.SessionManager` and
    shared with :class:`~toddler.agent.loop.AgentLoop` and
    :class:`~toddler.tools.executor.ToolExecutor` so they always see the
    same live mode without coupling to the state machine.

    Parameters
    ----------
    mode:
        Initial gating mode (defaults to :attr:`PermissionMode.MANUAL`).
    """

    def __init__(self, mode: PermissionMode = PermissionMode.MANUAL) -> None:
        self._mode = mode

    @property
    def mode(self) -> PermissionMode:
        """The current permission gating mode."""
        return self._mode

    def set_mode(self, mode: PermissionMode) -> None:
        """Replace the current permission gating mode."""
        self._mode = mode

    def needs_confirmation(self, perm: Permission) -> bool:
        """Return ``True`` when *perm* requires user confirmation under the
        current mode.

        This is the **single source of truth** for the permission gating
        policy — no mirrored logic to keep in sync.
        """
        if perm in (Permission.READ, Permission.SHELL_SAFE):
            return False
        if perm is Permission.WRITE:
            return self._mode is not PermissionMode.AUTO
        # SHELL_DANGEROUS and unknown — always confirm
        return True


# ---------------------------------------------------------------------------
# Call description — a parameter tool schemas carry unless the tool opts out
# ---------------------------------------------------------------------------

#: Key of the call-description parameter — injected by
#: :meth:`BaseTool.to_api_schema` into every schema but those of a tool
#: that declares ``require_description_parameter = False``.
CALL_DESCRIPTION_PARAM = "description"

#: Schema for that parameter — injected into the schema of every tool
#: that carries one, so one wording governs them all.
#: Copied per call: the dict is shared, and one downstream mutation would
#: corrupt the wording for every tool at once.
CALL_DESCRIPTION_PROPERTY = {
    "type": "string",
    "description": (
        "One line stating what this specific call does, in terms of the "
        "values you chose — e.g. \"replace the hardcoded session timeout "
        "in auth.py\". Not a restatement of the parameters."
    ),
}

#: Appended to the description of every tool that carries the parameter, at
#: schema time, so the model is told how to fill it wherever it reads such a
#: tool's schema.
CALL_DESCRIPTION_USAGE = (
    f"Always set '{CALL_DESCRIPTION_PARAM}' to a concise, precise line "
    "describing what this particular call does."
)


def _reject_declared_call_description(parameters: dict) -> None:
    """Raise when a tool declares the call-description property itself.

    Forbidden to every tool, opted out or not: the executor strips the
    key before ``execute`` and the panels hide it from the arguments, so
    a declaration that collides would silently lose its value.
    """
    if CALL_DESCRIPTION_PARAM in parameters.get("properties", {}):
        raise ValueError(
            f"A tool may not declare its own '{CALL_DESCRIPTION_PARAM}' "
            f"property — the base class owns that key."
        )


def _with_call_description(parameters: dict) -> dict:
    """Return *parameters* plus the required call-description property.

    Every level is rebuilt — a tool's ``parameters`` is a class attribute
    shared by all instances, so an in-place edit would mutate it for good
    (and grow ``required`` by one entry per call).  The property is placed
    first so the model emits it first, which is the order the panels render
    the arguments in.
    """
    _reject_declared_call_description(parameters)
    return {
        **parameters,
        "properties": {
            CALL_DESCRIPTION_PARAM: dict(CALL_DESCRIPTION_PROPERTY),
            **parameters.get("properties", {}),
        },
        # Not every tool declares a ``required`` list of its own —
        # synthesized here so the param is required on those too.
        "required": [CALL_DESCRIPTION_PARAM, *parameters.get("required", [])],
    }


def _without_call_description(parameters: dict) -> dict:
    """Return *parameters* as they ship for a tool that carries no
    call-description param.

    Nothing is merged, but the copy is still owed: ``parameters`` is a
    class attribute shared by every instance, and the provider holds on
    to the schema it is handed.  The same levels are rebuilt as
    :func:`_with_call_description` rebuilds, so both paths hand out
    schemas of the same shape.
    """
    _reject_declared_call_description(parameters)
    return {
        **parameters,
        "properties": dict(parameters.get("properties", {})),
        "required": list(parameters.get("required", [])),
    }


def execution_params(params: dict) -> dict:
    """Return a copy of *params* without the call-description param.

    A tool's ``execute`` takes its own arguments only.  The copy matters:
    ``call.parameters`` is the same object the tool-call events, the
    persisted transcript, and ``get_permission`` read, so the param must
    survive in it.
    """
    return {
        k: v for k, v in params.items() if k != CALL_DESCRIPTION_PARAM
    }


# ---------------------------------------------------------------------------
# Tool call / result
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A parsed tool-call request from the LLM."""

    tool_id: str
    tool_name: str
    parameters: dict


@dataclass
class ToolResult:
    """The outcome of executing a tool."""

    tool_id: str
    tool_name: str
    success: bool
    output: str
    checkpoint_id: str | None = None   # set when a checkpoint was created
    error: str | None = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# BaseTool ABC
# ---------------------------------------------------------------------------


class BaseTool(ABC):
    """Abstract base for every tool in the agent's toolbox.

    Subclasses must provide:
    - ``name`` (str)
    - ``description`` (str)
    - ``parameters`` (JSON Schema dict)
    - ``execute(**kwargs)`` → ToolResult

    They MAY override ``permission`` (default: READ),
    ``in_default_registry`` (default: True), ``visible`` (default:
    True) and ``require_description_parameter`` (default: True); a tool
    that touches files declares them in ``path_params`` (or overrides
    ``get_scope_paths``) so the gate can place the call.
    """

    name: str
    description: str
    parameters: dict  # JSON Schema for the tool's input

    #: Whether :meth:`to_api_schema` injects the call-description
    #: parameter.  A tool whose arguments already say what the call does —
    #: ``read_file`` names the file, ``plan_update`` the step and its new
    #: status — sets this ``False``, and its schema carries the tool's own
    #: parameters alone.
    require_description_parameter: bool = True

    #: Whether :func:`~toddler.tools.create_default_registry` hands this
    #: tool to every session.  Default tools declare nothing; a tool that
    #: only makes sense once a session arms it — ``plan_update``, live for
    #: a plan phase alone — sets this ``False``.  It still registers itself
    #: at definition time either way, so the catalog stays a complete
    #: inventory of the toolbox.
    in_default_registry: bool = True

    #: Whether the console draws a card for calls to this tool.  A tool
    #: whose effect already reaches the browser as a frame of its own —
    #: ``plan_update``, whose statuses arrive as ``plan_step_update`` —
    #: sets this ``False``, so the console says it once rather than twice.
    #: The call is still made, executed, and stored in the transcript;
    #: only its card is withheld.
    visible: bool = True

    #: Parameters naming a file or directory the call touches, for
    #: :meth:`get_scope_paths`.  A tool whose paths sit in arguments of
    #: another shape — ``Shell``, reading them out of its command string
    #: — overrides that method instead of declaring these.
    path_params: tuple[str, ...] = ()

    #: The session's :class:`FileScopePolicy`, handed in by
    #: :func:`~toddler.tools.create_default_registry`.  A class attribute
    #: so that a tool whose ``__init__`` never reaches this class — the
    #: definition-time catalog, a test double — still answers with the
    #: default policy instead of raising.
    _scope_policy: FileScopePolicy | None = None

    def __init__(self, *, scope_policy: FileScopePolicy | None = None) -> None:
        """Store the session's scope policy.

        A tool is constructed once, without knowing its session; the
        registry hands the policy in so that later calls can be placed
        against the workspace they were made in.
        """
        self._scope_policy = scope_policy

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with the given keyword arguments.

        ``kwargs`` is the deserialized JSON Schema properties — e.g.
        ``path="/foo/bar.txt"`` for a ReadFile tool.
        """
        ...

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    @property
    def permission(self) -> Permission:
        """Default permission is READ; override for mutating tools."""
        return Permission.READ

    def get_permission(self, **kwargs) -> Permission:  # noqa: ARG002
        """Resolve the effective permission for a specific invocation.

        Most tools return a static ``permission``, but tools like ``Shell``
        that need to inspect the parameters (e.g. the command string) can
        override this method to return a dynamic permission level.

        The executor always calls this method (not the property directly)
        so that dynamic classification works transparently.
        """
        return self.permission

    # ------------------------------------------------------------------
    # File scope
    # ------------------------------------------------------------------

    @property
    def scope_policy(self) -> FileScopePolicy:
        """The policy that places this tool's files.

        Handed in at construction; a tool that never received one — a
        bare tool in a test, the definition-time catalog — builds a
        default policy for the process working directory on first use.
        """
        if self._scope_policy is None:
            self._scope_policy = FileScopePolicy.default()
        return self._scope_policy

    def get_scope_paths(self, **kwargs) -> list[Path]:
        """The paths this invocation would touch, resolved.

        Default: whatever :attr:`path_params` names.  Overridden where
        the paths are not plain parameters — ``Shell`` reads them out of
        the command string — because only the tool knows what base its
        arguments resolve against.
        """
        paths: list[Path] = []
        for name in self.path_params:
            resolved = _resolve_path(kwargs.get(name))
            if resolved is not None:
                paths.append(resolved)
        return paths

    def get_file_scope(self, **kwargs) -> FileScope:
        """The effective :class:`FileScope` of this invocation.

        Asked alongside :meth:`get_permission` for every call.  A tool
        that names no file answers ``WORKSPACE``: there is no reach to
        place, and the permission tier alone then decides.
        """
        return self.scope_policy.classify_many(self.get_scope_paths(**kwargs))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_api_schema(self) -> dict:
        """Return the OpenAI tool-compatible schema dict.

        The call-description parameter and its usage line are merged in
        here (see :data:`CALL_DESCRIPTION_PARAM` and
        :data:`CALL_DESCRIPTION_USAGE`), so both hold by construction
        rather than by each tool remembering them — unless the tool
        declares :attr:`require_description_parameter` ``False``, and is
        handed back the parameters it declared.  Either way the tool's own
        ``parameters`` declaration is left untouched.

        Example::

            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from disk",
                    "parameters": {"type": "object", "properties": {...}}
                }
            }
        """
        description = self.description.rstrip()
        if self.require_description_parameter:
            parameters = _with_call_description(self.parameters)
            description = f"{description} {CALL_DESCRIPTION_USAGE}"
        else:
            parameters = _without_call_description(self.parameters)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": parameters,
            },
        }

    def summarize_call(self, **kwargs) -> str:
        """One-line summary of the call for display / logging.

        Default: ``"tool_name(key=val, ...)"``.
        """
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{self.name}({args})"
