"""Resuming a project's last session — the default with no session ID.

A session is bound to the absolute, symlink-resolved directory it was
started in.  Resolving without an ID resumes the most recently used
non-empty session of that directory; ``new_session=True`` opts out and a
directory with no history gets a fresh session bound to it.

A session is also claimed for the life of the process that resolves it:
a second process in the same project starts a fresh session rather than
appending to one somebody else is using.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.mocks import MockLLMProvider
from toddler.config.settings import Settings
from toddler.llm import Message
from toddler.session import (
    SessionInUseError,
    SessionManager,
    SQLiteDatabase,
    StorageManager,
)
from toddler.session.database import normalize_project_path
from toddler.session.models import Session


@pytest.fixture
def db(tmp_path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "sessions.db")
    database.open()
    return database


@pytest.fixture
def storage(db) -> StorageManager:
    return StorageManager(db)


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _manager(storage: StorageManager, repo: Path) -> SessionManager:
    return SessionManager(
        Settings(session_dir=repo.parent, streaming_enabled=False),
        storage,
        MockLLMProvider(),
        repo_root=repo,
    )


def _add_turn(storage: StorageManager, session: Session) -> None:
    """Give *session* one persisted message — "used", not startup residue."""
    conversation = storage.create_conversation(session.id)
    storage.append_message(
        session.id, Message.user("hi"), conversation_id=conversation.id,
    )


def _backdate(db: SQLiteDatabase, session: Session, *, hours: int) -> None:
    """Move *session* back in time.

    ``StorageManager.update`` stamps ``updated_at`` with "now", so a
    backdate has to go through the database layer to stick.
    """
    session.updated_at = datetime.now(UTC) - timedelta(hours=hours)
    db.update_session(session)


def _status_path(repo: Path, session_id: str) -> Path:
    """The claim file a manager built for *repo* would hold."""
    return repo.parent / "sessions" / f"{session_id}.json"


class TestResumeByProject:

    async def test_resumes_the_last_used_session_of_the_directory(
        self, storage, db, repo,
    ):
        first = storage.create(cwd=str(repo))
        _add_turn(storage, first)
        second = storage.create(cwd=str(repo))
        _add_turn(storage, second)
        _backdate(db, first, hours=1)

        mgr = _manager(storage, repo)
        await mgr.resolve()

        assert mgr.session.id == second.id
        assert mgr.conversation.session_id == second.id

    async def test_an_empty_session_does_not_shadow_the_last_used_one(
        self, storage, db, repo,
    ):
        """A crash leaves an empty session behind; resuming it would hide
        the conversation the user actually had."""
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)
        ghost = storage.create(cwd=str(repo))

        mgr = _manager(storage, repo)
        await mgr.resolve()

        assert mgr.session.id == used.id != ghost.id

    async def test_another_projects_session_is_not_resumed(
        self, storage, db, repo, tmp_path,
    ):
        other = tmp_path / "other"
        other.mkdir()
        foreign = storage.create(cwd=str(other))
        _add_turn(storage, foreign)

        mgr = _manager(storage, repo)
        await mgr.resolve()

        assert mgr.session.id != foreign.id
        assert mgr.session.project_path == normalize_project_path(repo)

    async def test_a_symlinked_directory_finds_the_same_sessions(
        self, storage, db, repo, tmp_path,
    ):
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        created = storage.create(cwd=str(link))
        _add_turn(storage, created)

        mgr = _manager(storage, repo)
        await mgr.resolve()

        assert mgr.session.id == created.id

    async def test_explicit_session_id_wins_over_the_project_default(
        self, storage, db, repo,
    ):
        older = storage.create(cwd=str(repo))
        _add_turn(storage, older)
        latest = storage.create(cwd=str(repo))
        _add_turn(storage, latest)

        mgr = _manager(storage, repo)
        await mgr.resolve(older.id)

        assert mgr.session.id == older.id

    async def test_new_session_skips_the_resume(self, storage, db, repo):
        existing = storage.create(cwd=str(repo))
        _add_turn(storage, existing)

        mgr = _manager(storage, repo)
        await mgr.resolve(new_session=True)

        assert mgr.session.id != existing.id
        assert mgr.session.project_path == normalize_project_path(repo)



class TestSessionLocking:
    """One live process per session — the claim file arbitrates."""

    async def test_a_second_process_opens_a_new_session(
        self, storage, db, repo,
    ):
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)

        first = _manager(storage, repo)
        await first.resolve()
        second = _manager(storage, repo)
        await second.resolve()

        assert first.session.id == used.id
        assert second.session.id != used.id

    async def test_close_removes_the_claim_file_and_frees_the_session(
        self, storage, db, repo,
    ):
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)
        status = _status_path(repo, used.id)

        first = _manager(storage, repo)
        await first.resolve()
        assert status.is_file()
        assert json.loads(status.read_text())["session_id"] == used.id

        await first.close()
        assert not status.exists()

        # The next process resumes what the first one released.
        second = _manager(storage, repo)
        await second.resolve()
        assert second.session.id == used.id

    async def test_an_explicit_id_held_elsewhere_is_refused(
        self, storage, db, repo,
    ):
        """``--session`` is an instruction: it fails rather than silently
        opening a different session."""
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)

        holder = _manager(storage, repo)
        await holder.resolve()

        other = _manager(storage, repo)
        with pytest.raises(SessionInUseError):
            await other.resolve(used.id)

    async def test_switching_to_a_held_session_keeps_the_current_one(
        self, storage, db, repo,
    ):
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)

        holder = _manager(storage, repo)
        await holder.resolve()

        other = _manager(storage, repo)
        await other.resolve()
        before = other.session.id

        with pytest.raises(SessionInUseError):
            await other.switch_session(used.id)

        assert other.session.id == before

    async def test_a_stale_claim_file_neither_blocks_nor_survives(
        self, storage, db, repo,
    ):
        """A crash releases the OS lock but leaves the file; the next
        process takes the session and the sweep removes the residue."""
        used = storage.create(cwd=str(repo))
        _add_turn(storage, used)
        stale = _status_path(repo, used.id)
        stale.parent.mkdir(parents=True)
        stale.write_text('{"session_id": "dead", "pid": 999999}')

        mgr = _manager(storage, repo)
        await mgr.resolve()

        assert mgr.session.id == used.id
        assert json.loads(stale.read_text())["session_id"] == used.id
        assert json.loads(stale.read_text())["pid"] != 999999
