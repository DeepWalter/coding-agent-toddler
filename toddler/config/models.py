"""Model naming — slots, the ``[1m]`` notation, and the budgets that follow.

A model is named by a *spec*: a vendor id, optionally carrying a bracketed
context suffix that is Toddler's own notation and never reaches the endpoint
(``"deepseek-flash[1m]"``).  Everything the rest of the agent needs to know
about the model is derived from that one string — which id to ask for, how
large a window to account for, and how much output one response may spend.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from toddler.config import defaults

__all__ = [
    "TurnConfig",
    "completion_budget_for",
    "context_length_for",
    "resolve_slot",
    "split_spec",
    "wire_model",
]

logger = logging.getLogger(__name__)

# A trailing "[...]" — the only place Toddler reads notation off a model name.
_SUFFIX_RE = re.compile(r"\[([^\]]*)\]$")

# Effort tiers, weakest to strongest, so a budget can be picked by rank.
# "none" is not a point on that scale (it disables thinking), so it is
# handled before the lookup.
_EFFORT_ORDER = {
    tier: rank for rank, tier in enumerate(defaults.REASONING_EFFORT_TIERS)
}


def split_spec(spec: str) -> tuple[str, str | None]:
    """Split *spec* into its model id and its trailing notation.

    ``"deepseek-flash[1m]"`` → ``("deepseek-flash", "1m")``; a spec without
    a suffix → ``(spec, None)``.  The suffix is never sent anywhere.
    """
    match = _SUFFIX_RE.search(spec)
    if match is None:
        return spec.strip(), None
    return spec[: match.start()].strip(), match.group(1).strip().lower()


def wire_model(spec: str) -> str:
    """The id to put on the wire — *spec* with its notation stripped."""
    return split_spec(spec)[0]


def context_length_for(spec: str) -> int:
    """The context window *spec* is accounted for.

    A known suffix names its own window.  An unrecognized one warns and
    falls back to the default *after stripping*: a bracket is never legal in
    a model id, so sending it would 404, and a window that is off is a better
    failure than a request that cannot be made at all.
    """
    _, suffix = split_spec(spec)
    if suffix is None:
        return defaults.DEFAULT_MAX_CONTEXT_LENGTH

    window = defaults.CONTEXT_SUFFIXES.get(suffix)
    if window is None:
        logger.warning(
            "Unknown context suffix %r in model %r — accounting for %d tokens.",
            suffix, spec, defaults.DEFAULT_MAX_CONTEXT_LENGTH,
        )
        return defaults.DEFAULT_MAX_CONTEXT_LENGTH
    return window


def completion_budget_for(effort: str | None) -> int:
    """How much output one response may spend at *effort*.

    Thinking is paid for out of the same budget as the answer, so the ends of
    the scale need different rooms.  An unrecognized tier takes the top
    budget: the provider coerces it to its own maximum, and the number here
    should describe what the endpoint will actually do.
    """
    if effort is None:
        return defaults.DEFAULT_MAX_COMPLETION_TOKENS

    tier = effort.strip().lower()
    if tier == "none":
        return defaults.NO_THINKING_MAX_COMPLETION_TOKENS

    top = _EFFORT_ORDER["max"]
    if _EFFORT_ORDER.get(tier, top) >= top:
        return defaults.TOP_EFFORT_MAX_COMPLETION_TOKENS
    return defaults.DEFAULT_MAX_COMPLETION_TOKENS


def resolve_slot(selection: str, slots: Mapping[str, str]) -> str:
    """Resolve a slot name to the model spec it names.

    Case-insensitive, so ``/model Pro`` works.  An unknown name raises: the
    callers differ in how loud they can be — the CLI parser rejects it at
    ``choices=``, the settings loader warns and falls back because it runs at
    import time, and ``/model`` reports it to the user.
    """
    try:
        return slots[selection.strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown model slot {selection!r}; "
            f"expected one of {', '.join(sorted(slots))}"
        ) from None


@dataclass(frozen=True, slots=True)
class TurnConfig:
    """Everything one agent turn needs to know about the model it runs on.

    Owned by the session: :meth:`SessionManager.process_turn` resolves it
    once and threads it down, so every request a turn makes — planning,
    exploration, the plan proposal, each execution round — carries the same
    config and a selection change mid-turn cannot split the turn across two
    models.

    ``spec`` is the only identity.  Display, the conversation row, and the
    token-baseline cache key all hold it; the three members derived from it
    are properties rather than fields, so a config cannot carry a model and a
    window that disagree, and ``dataclasses.replace`` is the whole of a
    selection change.

    Parameters
    ----------
    spec:
        The model name in Toddler's notation — a vendor id, optionally with
        a bracketed context suffix (``"deepseek-flash[1m]"``).
    reasoning_effort:
        How much thinking the model may spend before answering, on the
        shared tier scale (``"minimal"`` … ``"max"``; ``"none"`` disables
        thinking).  *None* omits the field, leaving the endpoint's own
        default in place.
    """

    spec: str
    reasoning_effort: str | None = None

    @property
    def model(self) -> str:
        """The id sent on the wire — ``spec`` with its notation stripped."""
        return wire_model(self.spec)

    @property
    def max_context_tokens(self) -> int:
        """The context window this model is accounted for."""
        return context_length_for(self.spec)

    @property
    def max_completion_tokens(self) -> int:
        """How much output one response may spend, thinking included."""
        return completion_budget_for(self.reasoning_effort)
