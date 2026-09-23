"""Base tool abstractions — Permission, ToolResult, BaseTool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Permission(Enum):
    """Permission tier for a tool.

    Used by the ``ToolExecutor`` to decide whether auto-approval is allowed
    or user confirmation is needed.
    """

    READ = "read"               # side-effect-free reads (auto-approve)
    WRITE = "write"             # file mutations (confirm by default)
    SHELL_SAFE = "shell_safe"   # safe shell commands — ls, git status, etc.
    SHELL_DANGEROUS = "shell_dangerous"  # rm, sudo, curl, etc. (always confirm)  # noqa: E501


class PermissionMode(Enum):
    """Permission gating policy for tool execution.

    ``MANUAL`` (the default) auto-approves READ + SHELL_SAFE and
    requires confirmation for WRITE + SHELL_DANGEROUS.  ``AUTO``
    additionally auto-approves WRITE, leaving only SHELL_DANGEROUS
    to confirm.
    """

    MANUAL = "manual"
    AUTO = "auto"


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
# Call description — the one parameter every tool call carries
# ---------------------------------------------------------------------------

#: Key of the parameter every tool call carries.
CALL_DESCRIPTION_PARAM = "description"

#: Schema for that parameter — injected into every tool's schema by
#: :meth:`BaseTool.to_api_schema` so one wording governs every tool.
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

#: Appended to every tool's description at schema time, so the model is told
#: how to fill the parameter wherever it reads a tool's schema.
CALL_DESCRIPTION_USAGE = (
    f"Always set '{CALL_DESCRIPTION_PARAM}' to a concise, precise line "
    "describing what this particular call does."
)


def _with_call_description(parameters: dict) -> dict:
    """Return *parameters* plus the required call-description property.

    Every level is rebuilt — a tool's ``parameters`` is a class attribute
    shared by all instances, so an in-place edit would mutate it for good
    (and grow ``required`` by one entry per call).  The property is placed
    first so the model emits it first, which is the order the panels render
    the arguments in.
    """
    properties = parameters.get("properties", {})
    if CALL_DESCRIPTION_PARAM in properties:
        raise ValueError(
            f"A tool may not declare its own '{CALL_DESCRIPTION_PARAM}' "
            f"property — the base class adds it to every schema."
        )
    return {
        **parameters,
        "properties": {
            CALL_DESCRIPTION_PARAM: dict(CALL_DESCRIPTION_PROPERTY),
            **properties,
        },
        # Not every tool declares a ``required`` list of its own —
        # synthesized here so the param is required on those too.
        "required": [CALL_DESCRIPTION_PARAM, *parameters.get("required", [])],
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

    They MAY override ``permission`` (default: READ).
    """

    name: str
    description: str
    parameters: dict  # JSON Schema for the tool's input

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
    # Serialisation
    # ------------------------------------------------------------------

    def to_api_schema(self) -> dict:
        """Return the OpenAI tool-compatible schema dict.

        The call-description parameter and its usage line are merged in
        here (see :data:`CALL_DESCRIPTION_PARAM` and
        :data:`CALL_DESCRIPTION_USAGE`), so both hold for every tool by
        construction rather than by each tool remembering them.  The
        tool's own ``parameters`` declaration is left untouched.

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
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    f"{self.description.rstrip()} {CALL_DESCRIPTION_USAGE}"
                ),
                "parameters": _with_call_description(self.parameters),
            },
        }

    def summarize_call(self, **kwargs) -> str:
        """One-line summary of the call for display / logging.

        Default: ``"tool_name(key=val, ...)"``.
        """
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{self.name}({args})"
