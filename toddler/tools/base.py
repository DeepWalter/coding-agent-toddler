"""Base tool abstractions — the tool ABC and its call/result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from toddler.tools.policy import (
    FileScope,
    FileScopePolicy,
    Permission,
    display_path,
    expand_path,
)
from toddler.tools.prompts import (
    ALLOW_CALL_TITLE,
    DEFAULT_SINGLE_CHOICES,
    PromptChoices,
    ToolCallPrompt,
)

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
    True) and ``require_description_parameter`` (default: True); a
    tool that touches files declares them in ``path_params`` (or
    overrides ``get_scope_paths``) so the gate can place the call.
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

        The loop's gate always calls this method (not the property
        directly) so that dynamic classification works transparently.
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
        """The paths this invocation would touch.

        Default: whatever :attr:`path_params` names.  Overridden where
        the paths are not plain parameters — ``Shell`` reads them out of
        the command string — because only the tool knows what base its
        arguments resolve against.

        The paths are named as they were given, not resolved: the policy
        resolves them itself before comparing, and a refusal reads better
        when it names the path the caller used than the one the
        filesystem knows it by.
        """
        paths: list[Path] = []
        for name in self.path_params:
            path = expand_path(kwargs.get(name))
            if path is not None:
                paths.append(path)
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

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The gate card's title for this call.

        Default: :data:`ALLOW_CALL_TITLE`.  A tool whose call reads better
        named for what it is — a file to read, a command to run —
        overrides this rather than the whole prompt.
        """
        return ALLOW_CALL_TITLE

    def _prompt_content(self, **kwargs) -> str | None:  # noqa: ARG002
        """The call's own body, for a card that boxes one, or ``None``.

        Default: ``None``.  Only a tool whose call *is* a body — ``Shell``,
        whose command is the thing being approved — hands one over.
        """
        return None

    def _prompt_choices(self, **kwargs) -> PromptChoices:  # noqa: ARG002
        """The rows the pause offers, or the questions it asks.

        Default: the ordinary approve/deny/feedback gate
        (:data:`DEFAULT_SINGLE_CHOICES`).  A tool varies the wording or
        drops a row by overriding this; an ask-user tool returns a
        ``QUESTION`` payload instead.
        """
        return DEFAULT_SINGLE_CHOICES

    def _prompt_description(self, **kwargs) -> str | None:
        """The call's own line about what it does, or ``None``.

        In order:

        1. the ``description`` parameter the model wrote, where the tool's
           schema carries one — the call's own words, which is what the
           user is being asked to approve;
        2. otherwise the display path of the first path parameter the call
           actually set, spelled by :func:`display_path`: a call that
           names a file is about that file, and the path is the only thing
           that says which one;
        3. otherwise :meth:`summarize_call`, so a call declaring neither
           still names itself rather than drawing a card that says nothing
           at all.

        An argument of the wrong shape — the model sent a number where a
        path belongs — falls through to the next tier rather than raising,
        for the same reason :func:`_expand_path` refuses it: a classifier
        runs on model-authored arguments, and a value the tool cannot use
        must not take the turn down at the gate that offers to run it.
        """
        described = kwargs.get(CALL_DESCRIPTION_PARAM)
        if described and str(described).strip():
            return str(described).strip()
        for name in self.path_params:
            if not kwargs.get(name):
                continue
            try:
                return str(display_path(kwargs[name]))
            except (TypeError, RuntimeError, ValueError):
                continue
        try:
            return self.summarize_call(**kwargs)
        except (TypeError, RuntimeError, ValueError):
            # Summary too — the tool's name is the last thing the call can
            # be named by, and a card that says only that is still a card
            # the user can answer.
            return self.name

    def build_call_prompt(self, **kwargs) -> ToolCallPrompt:
        """The prompt the gate card renders for this call.

        Assembled from the ``_prompt_*`` helpers, so a tool with
        something to say about one part overrides that helper and leaves
        the others alone.

        A sibling of :meth:`summarize_call`, not a replacement: that one
        is still the line :func:`blocked_message` and the checkpoint
        callback write down.
        """
        return ToolCallPrompt(
            title=self._prompt_title(**kwargs),
            content=self._prompt_content(**kwargs),
            description=self._prompt_description(**kwargs),
            choices=self._prompt_choices(**kwargs),
        )
