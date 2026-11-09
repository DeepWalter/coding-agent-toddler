"""Permission and file-scope policy — what may run, and where it may reach.

:class:`PermissionManager` answers the tier question — read, write,
shell, interact — and :class:`FileScopePolicy` answers the reach
question — workspace, external, restricted.  The session builds one of
each and hands them to the loop and the tools; the gate asks both about
every call, and this module is the only place either answer is decided.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.tools.base import BaseTool

__all__ = [
    "Decision",
    "FileScope",
    "FileScopePolicy",
    "Permission",
    "PermissionManager",
    "PermissionMode",
    "blocked_message",
    "display_path",
    "expand_path",
]


class Permission(Enum):
    """Permission tier for a tool.

    Read by the loop's gate to decide whether auto-approval is allowed or
    user confirmation is needed.  ``INTERACT`` always pauses: it asks the
    user for input rather than for permission.
    """

    READ = "read"               # side-effect-free reads (auto-approve)
    WRITE = "write"             # file mutations (confirm by default)
    SHELL_SAFE = "shell_safe"   # safe shell commands — ls, git status, etc.
    SHELL_DANGEROUS = "shell_dangerous"  # rm, sudo, curl, etc. (always confirm)  # noqa: E501
    INTERACT = "interact"       # asks the user — input, always paused

    @property
    def is_mutating(self) -> bool:
        """Whether a call of this tier changes state.

        These are the tiers a checkpoint is taken before: something is
        about to be touched, and the checkpoint is what undoes it.
        ``READ``, ``SHELL_SAFE`` and ``INTERACT`` leave nothing to undo.
        """
        return self in (Permission.WRITE, Permission.SHELL_DANGEROUS)


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
    reaches outside asks, whatever the mode.  An ``INTERACT`` call
    asks in both modes: the pause is input, not a risk decision.
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


def expand_path(
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
    path = expand_path(value, base=base)
    if path is None:
        return None
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def display_path(value: str | os.PathLike[str]) -> Path:
    """*value* as an absolute path with the links left where they are.

    What a tool opens is the resolved path; what a message should name is
    the path the caller wrote.  On macOS whole trees differ between the
    two — ``/tmp`` is ``/private/tmp``, ``/home`` is
    ``/System/Volumes/Data/home`` — and someone looking for the file they
    named is served better by the spelling they used.

    A path whose ``..`` stands after a link is the exception, and is
    resolved: the kernel reads ``link/..`` as the parent of what the link
    points at, not of the link, so the spelling as written would name a
    file the call never touches.
    """
    try:
        path = Path(value).expanduser()
    except (RuntimeError, ValueError):
        # ``~user`` for a user this machine has not got, or a NUL — the
        # caller's own handling refuses it; there is nothing to name.
        return Path(str(value))
    if ".." in path.parts:
        return path.resolve()
    return Path(os.path.abspath(path))


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
    path = expand_path(value, base=base)
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
        ``~/.toddler/.env``), and ``tools.policy`` is imported by nearly
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
        problem.  Named as it was given, for the same reason messages
        read the written path everywhere else.
        """
        for path in paths:
            if self.classify(path) is FileScope.RESTRICTED:
                return Path(str(path))
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
    """The session's gating policy — the mode, and what it decides.

    Owned by :class:`~toddler.session.manager.SessionManager` and shared
    with :class:`~toddler.agent.loop.AgentLoop`, so the loop always sees
    the live mode without coupling to the state machine.  The loop's gate
    is the only caller of :meth:`decide`, and it is the only thing
    standing between a tool call and its execution.

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

    def decide(self, perm: Permission, scope: FileScope) -> Decision:
        """What to do with a call of tier *perm* reaching *scope*.

        This is the **single source of truth** for the gating policy —
        no mirrored logic to keep in sync.

        An ``INTERACT`` call is read first and asks whatever the mode
        and scope: the pause is input, not a risk decision — the call
        reads the user, not the filesystem.  Otherwise scope is read
        first.  A restricted file is refused whatever the tier and
        whatever the mode — ``READ`` included, ``AUTO`` included — and
        no approval lifts it: it is policy, not a question.  Reaching
        outside the workspace is never automatic either, though for a
        different reason: how far past the ground the agent was given is
        the user's decision to make, and the answer to an unmade
        decision is to ask.  Inside the workspace the tiers hold, with
        ``AUTO`` adding ``WRITE``.
        """
        if perm is Permission.INTERACT:
            return Decision.ASK
        if scope is FileScope.RESTRICTED:
            return Decision.DENY
        if scope is FileScope.EXTERNAL:
            return Decision.ASK
        if perm in (Permission.READ, Permission.SHELL_SAFE):
            return Decision.ALLOW
        if perm is Permission.WRITE and self._mode is PermissionMode.AUTO:
            return Decision.ALLOW
        # WRITE under MANUAL, SHELL_DANGEROUS in both, and any tier a
        # later tool introduces — a new tier has to ask before it may run.
        return Decision.ASK


def blocked_message(tool: BaseTool, params: dict) -> str:
    """What a call refused by the restricted policy reports.

    It reaches the model as the call's result and stays in the transcript,
    so it names the path that was refused and says that retrying is
    pointless — a bare refusal invites the same call, spelled differently.
    """
    match = tool.scope_policy.restricted_match(tool.get_scope_paths(**params))
    where = f"'{match}'" if match is not None else "a restricted path"
    return (
        f"Blocked: {tool.summarize_call(**params)} touches {where}, which "
        f"is restricted and cannot be read or written. The call was not "
        f"executed and cannot be approved — tell the user this path is "
        f"off-limits instead of retrying it."
    )
