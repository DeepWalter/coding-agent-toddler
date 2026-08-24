"""TurnRunner — owns the ``process_turn`` generator for the web frontend.

One runner per active session (created in the app lifespan).  It runs
``SessionManager.process_turn`` inside an **app-lifetime asyncio.Task** —
never owned by a WebSocket connection — so a disconnected tab keeps the
turn alive while other tabs keep watching.

Events are serialized once and broadcast into per-connection
``asyncio.Queue``s (multi-tab watching works for free); each connection
drains its own queue in a separate task.

The runner also snapshots ``AgentPaused`` payloads so reconnecting clients
can resume a pending approval (delivered in ``hello``), and auto-cancels a
paused turn when the last watcher leaves — a pending approval nobody can
answer would hang forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    AgentPaused,
    FatalAgentError,
)
from toddler.session import SessionManager
from toddler.web.events import serialize_event

__all__ = ["TurnRunner"]

logger = logging.getLogger(__name__)


class TurnRunner:
    """Run one ``process_turn`` generator per active session, broadcasting
    serialized events to all subscribed WebSocket connections.
    """

    def __init__(self, session_mgr: SessionManager) -> None:
        self._session_mgr = session_mgr

        # The busy gate: acquired synchronously in start() and released in
        # the runner task's finally — while held, no second turn can start
        # (SessionManager holds single-session state).
        self._lock = asyncio.Lock()

        # App-lifetime task consuming process_turn.  Never awaited by a
        # connection; disconnects unsubscribe, they don't kill the turn.
        self._task: asyncio.Task | None = None
        self._gen: AsyncIterator[AgentEvent] | None = None

        # Per-connection event queues (queue_id → queue).
        self._subscribers: dict[int, asyncio.Queue[dict]] = {}
        self._next_queue_id = 0

        # Snapshot of the last AgentPaused frame — delivered to
        # reconnecting clients in hello; cleared when the turn ends.
        self._paused_snapshot: dict | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while a turn is running (the busy gate is held)."""
        return self._lock.locked()

    @property
    def paused_snapshot(self) -> dict | None:
        """The pending ``agent_paused`` frame, or *None* when not paused."""
        return self._paused_snapshot

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self) -> tuple[int, asyncio.Queue[dict]]:
        """Register a new connection; returns ``(queue_id, queue)``."""
        self._next_queue_id += 1
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers[self._next_queue_id] = queue
        return self._next_queue_id, queue

    def unsubscribe(self, queue_id: int) -> None:
        """Drop a connection's queue.

        Stranded-approval guard: when the last watcher leaves while the
        agent is paused, auto-cancel the turn — a pending approval nobody
        can answer would hang forever.
        """
        self._subscribers.pop(queue_id, None)
        if self._paused_snapshot is not None and not self._subscribers:
            logger.info("Last watcher left a paused turn — cancelling.")
            self.cancel()

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    async def start(
        self, user_input: str, *, force_plan: bool = False,
    ) -> bool:
        """Start a turn; returns ``False`` when a turn is already running.

        The busy gate is an ``asyncio.Lock`` held for the whole turn:
        acquired here and released in the runner task's ``finally``.
        There is no await between the ``locked()`` check and the acquire,
        so no other task can take the lock in between — awaiting the free
        lock's coroutine completes immediately, without suspending.
        """
        if self._lock.locked():
            return False
        await self._lock.acquire()
        self._task = asyncio.create_task(
            self._run(user_input, force_plan=force_plan),
        )
        return True

    def cancel(self) -> bool:
        """Cancel the running turn.

        The ``turn_cancelled`` broadcast, generator close, snapshot clear,
        and ``state {busy: false}`` all happen inside the task itself
        (see :meth:`_run`).  Returns ``True`` when a turn was in flight.
        """
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _broadcast(self, frame: dict) -> None:
        """Enqueue *frame* for every subscribed connection.

        Queues are unbounded, so ``put_nowait`` cannot raise
        ``QueueFull``; a slow consumer just builds a backlog.
        """
        for queue in self._subscribers.values():
            queue.put_nowait(frame)

    async def _run(self, user_input: str, *, force_plan: bool) -> None:
        """Consume the ``process_turn`` generator and broadcast its events.

        The generator is stored on the runner so cancellation can close
        it explicitly; the frame sequence contract for a cancelled turn is
        ``turn_cancelled`` then ``state {busy: false}``, matching the
        natural ``agent_finished`` → ``state`` order of a completed turn.
        """
        self._broadcast({"type": "turn_started"})
        self._broadcast({"type": "state", "busy": True})

        gen = self._session_mgr.process_turn(
            user_input, force_plan=force_plan,
        )
        self._gen = gen
        try:
            async for event in gen:
                frame = serialize_event(event)
                if frame is None:
                    continue
                if isinstance(event, AgentPaused):
                    self._paused_snapshot = frame
                elif isinstance(event, (AgentFinished, FatalAgentError)):
                    self._paused_snapshot = None
                self._broadcast(frame)
        except asyncio.CancelledError:
            self._broadcast({"type": "turn_cancelled"})
            raise
        except Exception as exc:
            # A bug must not silently kill the task: surface it to the
            # clients (and the server log) instead of letting the task
            # die unobserved.  The finally block still resets state.
            logger.exception("Turn failed with an unexpected exception.")
            self._broadcast({"type": "fatal_error", "message": str(exc)})
        finally:
            # Safe no-op on a generator already stopped by cancellation.
            with contextlib.suppress(BaseException):
                await gen.aclose()
            self._gen = None
            self._paused_snapshot = None
            self._broadcast({"type": "state", "busy": False})
            self._lock.release()
