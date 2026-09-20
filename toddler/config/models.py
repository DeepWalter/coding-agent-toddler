"""The model config one agent turn runs with."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TurnConfig"]


@dataclass(frozen=True, slots=True)
class TurnConfig:
    """The ``(model, reasoning_effort)`` pair one agent turn runs with.

    Owned by the session: :meth:`SessionManager.process_turn` resolves it
    once and threads it down, so every request a turn makes — planning,
    exploration, the plan proposal, each execution round — carries the
    same pair and a selection change mid-turn cannot split the turn across
    two models.

    It sits beside the model settings rather than next to the turn that
    owns it, because it is built from them and because every layer that
    needs the annotation can then import it: the session layer already
    imports the agent layer, so a definition down there would be reachable
    from above only under ``TYPE_CHECKING``.

    Parameters
    ----------
    model:
        The model id sent on the wire.
    reasoning_effort:
        How much thinking the model may spend before answering, on the
        shared tier scale (``"minimal"`` … ``"max"``; ``"none"`` disables
        thinking).  *None* omits the field, leaving the endpoint's own
        default in place.
    """

    model: str
    reasoning_effort: str | None = None
