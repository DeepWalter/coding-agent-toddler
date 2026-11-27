"""Renderer tests — plan step status display and the reasoning view."""

from __future__ import annotations

import io

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from toddler.agent.events import (
    AgentFinished,
    AgentPaused,
    ContentDelta,
    ReasoningDelta,
    TodoUpdate,
    ToolCallEnd,
    ToolCallStart,
    ToolUseStart,
)
from toddler.cli.renderer import (
    ConfirmResult,
    NonStreamingRenderer,
    StreamingRenderer,
)
from toddler.llm.responses import TokenUsage
from toddler.tools.base import ToolResult
from toddler.tools.prompts import Question, QuestionChoice, ToolCallPrompt
from toddler.tools.todo import TodoItem, TodoStatus


class _FakeClock:
    """A monotonic stand-in the tests advance by hand.

    The renderer takes its clock as a parameter so a span can be asserted
    exactly, instead of racing the real one.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestNonStreamingTodoUpdates:
    """The one-shot todo policy prints live and closing snapshots only."""

    def _renderer(self):
        buf = io.StringIO()
        return NonStreamingRenderer(console=Console(file=buf)), buf

    def test_a_running_item_prints_the_whole_list(self):
        renderer, buf = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.IN_PROGRESS),
            TodoItem("Second thing", TodoStatus.PENDING),
        )))
        output = buf.getvalue()
        assert "▶️ First thing" in output
        assert "⬜ Second thing" in output

    def test_an_all_completed_list_prints(self):
        renderer, buf = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.COMPLETED),
            TodoItem("Second thing", TodoStatus.COMPLETED),
        )))
        assert "✅ First thing" in buf.getvalue()

    def test_an_all_pending_list_is_folded(self):
        renderer, buf = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.PENDING),
        )))
        assert buf.getvalue() == ""

    def test_an_empty_list_prints_nothing(self):
        renderer, buf = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=()))
        assert buf.getvalue() == ""


class TestStreamingTodoUpdates:
    """The TUI handler replaces its todo snapshot from each event; the
    panel is built later.
    """

    def _renderer(self) -> StreamingRenderer:
        return StreamingRenderer(console=Console(file=io.StringIO()))

    def test_todo_update_seeds_the_list(self):
        renderer = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.IN_PROGRESS),
        )))
        assert renderer._todos == [
            TodoItem("First thing", TodoStatus.IN_PROGRESS),
        ]

    def test_todo_panel_hidden_without_updates(self):
        renderer = self._renderer()
        assert renderer._build_todo_panel() is None

    def test_todo_panel_rows_show_status_and_do_not_wrap(self):
        renderer = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("Todo one", TodoStatus.IN_PROGRESS),
        )))
        renderer._max_todos_visible = 1
        panel = renderer._build_todo_panel()
        assert panel is not None
        assert str(panel.title) == "Todos"
        rows = panel.renderable.renderables
        assert len(rows) == 1
        row = rows[0]
        assert row.plain == "▶️ Todo one"
        # Single-line contract: the height budget counts one line per row.
        assert row.no_wrap is True
        assert row.overflow == "ellipsis"

    def test_cleared_todos_hide_the_panel(self):
        renderer = self._renderer()
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("Todo one", TodoStatus.IN_PROGRESS),
        )))
        renderer.on_todo_update(TodoUpdate(items=()))
        renderer._max_todos_visible = 1
        assert renderer._build_todo_panel() is None


class TestDynamicPanelHeightBudget:
    """The panel budget is charged only by panels that actually render.

    ``_compute_dynamic_panel_height()`` costs each panel its chrome plus
    rows; the todo list must fit at least one row past its chrome before
    the charge applies, so an empty list never drains the pool the tools
    panel draws from.
    """

    def _renderer(self, height: int) -> StreamingRenderer:
        return StreamingRenderer(
            console=Console(file=io.StringIO(), height=height)
        )

    def test_one_todo_row_fits_at_chrome_plus_one(self):
        # height 14: budget 10, minus the 5-line output minimum leaves 5
        # for extras — exactly the 4 chrome lines + 1 todo row.
        renderer = self._renderer(height=14)
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.IN_PROGRESS),
        )))
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_todos_visible == 1
        # The todo panel (chrome + 1 row) is charged, leaving output at
        # its 5-line minimum.
        assert output_height == 5

    def test_an_empty_list_never_charges_the_panel(self):
        # height 15: 6 rows for extras.  An empty list must not charge
        # chrome — otherwise the 6 rows that would show the first tool
        # are drained and the tools panel is dropped.
        renderer = self._renderer(height=15)
        renderer.on_todo_update(TodoUpdate(items=()))
        renderer.on_tool_use_start(
            ToolUseStart(tool_id="t1", tool_name="read")
        )
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_todos_visible == 0
        assert renderer._max_tools_visible == 1
        assert output_height == 5

    def test_todo_dropped_when_only_chrome_fits_leaves_budget_intact(self):
        # height 13: 4 rows for extras.  One todo row needs 5, so it is
        # dropped — and because nothing is charged, the output panel
        # keeps the whole budget instead of losing the panel's chrome.
        renderer = self._renderer(height=13)
        renderer.on_todo_update(TodoUpdate(items=(
            TodoItem("First thing", TodoStatus.IN_PROGRESS),
        )))
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_todos_visible == 0
        assert output_height == 9


# ============================================================================
# Reasoning (thinking mode)
# ============================================================================


class TestStreamingReasoning:
    """The thinking accumulator, its collapsed line, and the dismiss view."""

    @staticmethod
    def _renderer() -> StreamingRenderer:
        return StreamingRenderer(console=Console(file=io.StringIO()))

    @staticmethod
    def _texts(renderable: Group) -> list[str]:
        return [el.plain for el in renderable.renderables if isinstance(el, Text)]

    @staticmethod
    def _titles(renderable: Group) -> list[str]:
        return [
            el.title for el in renderable.renderables if isinstance(el, Panel)
        ]

    def test_content_delta_accumulates(self):
        renderer = self._renderer()
        renderer.on_content_delta(ContentDelta(text_delta="Hel"))
        renderer.on_content_delta(ContentDelta(text_delta="lo"))
        assert renderer._text == "Hello"

    def test_reasoning_delta_accumulates_verbatim(self):
        renderer = self._renderer()
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="step 1. "))
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="step 2."))
        assert renderer._thinking == "step 1. step 2."

    def test_collapsed_line_while_streaming(self):
        renderer = self._renderer()
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="cot"))

        renderable = renderer._build_renderable()
        # Four characters is the estimator's floor: one token, singular.
        assert "💭 Thinking… · 1 token" in self._texts(renderable)
        assert self._titles(renderable) == ["Output"]

    def test_collapsed_line_counts_the_streamed_buffer(self):
        renderer = self._renderer()
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="x" * 4200))

        renderable = renderer._build_renderable()
        # 4200 ASCII characters estimate at 1050 tokens — the same boundary
        # the web console prints as 1.1K.
        assert "💭 Thinking… · 1.1K tokens" in self._texts(renderable)

    def test_no_thinking_line_without_reasoning(self):
        renderer = self._renderer()
        renderer.on_content_delta(ContentDelta(text_delta="plain"))

        renderable = renderer._build_renderable()
        assert not any("💭" in text for text in self._texts(renderable))

    def test_dismiss_line_offers_the_toggle(self):
        renderer = self._renderer()
        renderer._thinking = "cot"
        renderer._dismiss_prompt = True

        renderable = renderer._build_renderable()
        # No clock was ever started, so the span reads as the floor.
        assert (
            "💭 Thought for less than a second — press t to review"
            in self._texts(renderable)
        )
        assert any("t to view thinking" in text for text in self._texts(renderable))
        assert self._titles(renderable) == ["Output"]

    def test_dismiss_line_reports_the_span(self):
        clock = _FakeClock()
        renderer = StreamingRenderer(
            console=Console(file=io.StringIO()), clock=clock,
        )
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="weigh"))
        clock.advance(12.4)
        renderer.on_content_delta(ContentDelta(text_delta="the answer"))
        renderer._dismiss_prompt = True

        renderable = renderer._build_renderable()
        assert (
            "💭 Thought for 12 seconds — press t to review"
            in self._texts(renderable)
        )

    def test_span_accumulates_across_rounds(self):
        # A tool call ends one round and the model thinks again: the span
        # is the sum, not the wall-clock distance between the first and
        # last fragment.
        clock = _FakeClock()
        renderer = StreamingRenderer(
            console=Console(file=io.StringIO()), clock=clock,
        )
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="first"))
        clock.advance(3.0)
        renderer.on_tool_use_start(
            ToolUseStart(tool_id="t1", tool_name="read")
        )
        clock.advance(30.0)          # tool runs — not thinking time
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="second"))
        clock.advance(5.0)
        renderer.on_content_delta(ContentDelta(text_delta="answer"))

        assert renderer._thinking_span() == 8.0

    def test_dismiss_view_shows_the_thinking_panel(self):
        renderer = self._renderer()
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="weigh options"))
        renderer.on_content_delta(ContentDelta(text_delta="the answer"))
        renderer._dismiss_prompt = True
        renderer._dismiss_view = "thinking"

        renderable = renderer._build_renderable()
        assert self._titles(renderable) == ["Thinking"]
        # The toggle hint flips, and the collapsed line gives way to the
        # panel.
        assert any("t to view output" in text for text in self._texts(renderable))
        assert not any("💭" in text for text in self._texts(renderable))

    def test_dismiss_hint_unchanged_without_reasoning(self):
        renderer = self._renderer()
        renderer._dismiss_prompt = True

        renderable = renderer._build_renderable()
        assert "\nPress Enter to continue…" in self._texts(renderable)

    def test_dismiss_view_resets_on_stop(self):
        renderer = self._renderer()
        renderer._thinking = "cot"
        renderer._dismiss_view = "thinking"
        # _wait_for_dismiss() is a no-op on this piped stdin, so stop()
        # runs its whole dismissal path.
        renderer.start()
        renderer.stop()
        assert renderer._dismiss_view == "output"

    def test_dismiss_skips_the_tty_path_on_piped_stdin(self, monkeypatch):
        """The termios ioctls fail on a pipe — the guard must return before
        touching them (``fileno`` asserts if the raw path is entered)."""

        class _PipedStdin:
            def isatty(self) -> bool:
                return False

            def fileno(self):
                raise AssertionError("termios path taken on piped stdin")

        monkeypatch.setattr("sys.stdin", _PipedStdin())
        renderer = self._renderer()
        renderer._thinking = "cot"
        renderer._wait_for_dismiss()

    def test_flush_summarises_the_thinking(self):
        buf = io.StringIO()
        renderer = StreamingRenderer(console=Console(file=buf))
        # The real app sequence: start the turn, then stop() (which the
        # piped stdin dismisses at once) before flushing to scrollback.
        renderer.start(turn_number=1)
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="weigh options"))
        renderer.on_content_delta(ContentDelta(text_delta="the answer"))
        renderer.stop()
        renderer.flush_to_console()

        output = buf.getvalue()
        # The deltas arrive in the same instant, so the span is the floor.
        assert "💭 Thought — less than a second" in output
        assert "the answer" in output
        # The live collapsed line does not leak into the scrollback.
        assert "💭 Thinking…" not in output

    def test_flush_summary_reports_the_span(self):
        clock = _FakeClock()
        buf = io.StringIO()
        renderer = StreamingRenderer(console=Console(file=buf), clock=clock)
        renderer.start(turn_number=1)
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="weigh options"))
        clock.advance(125.4)
        renderer.on_content_delta(ContentDelta(text_delta="the answer"))
        renderer.stop()
        renderer.flush_to_console()

        assert "💭 Thought — 2 minutes 5 seconds" in buf.getvalue()


class TestNonStreamingReasoning:
    """Plain scrollback cannot collapse — the thought prints in full."""

    @staticmethod
    def _capturing_renderer() -> tuple[NonStreamingRenderer, list]:
        printed: list = []
        renderer = NonStreamingRenderer(console=Console(file=io.StringIO()))
        renderer._console.print = lambda *args, **kwargs: printed.append(args[0])
        return renderer, printed

    def test_reasoning_prints_dim_and_verbatim(self):
        renderer, printed = self._capturing_renderer()
        renderer.on_reasoning_delta(ReasoningDelta(text_delta="weigh options"))

        assert [item.plain for item in printed] == [
            "💭 Thought:", "weigh options",
        ]
        assert printed[0].style == "dim"
        assert printed[1].style == "dim italic"

    def test_content_delta_still_renders_markdown(self):
        renderer, printed = self._capturing_renderer()
        renderer.on_content_delta(ContentDelta(text_delta="**bold**"))

        assert len(printed) == 1
        assert isinstance(printed[0], Markdown)


class TestUsageLine:
    """The turn's closing Tokens line, where the API's own reasoning count
    lands — the one number the live estimate never claims to be."""

    @staticmethod
    def _output(usage: TokenUsage | None) -> str:
        buf = io.StringIO()
        renderer = NonStreamingRenderer(console=Console(file=buf))
        renderer.on_agent_finished(AgentFinished(reason="completed", usage=usage))
        return buf.getvalue()

    def test_reasoning_tokens_are_itemized(self):
        usage = TokenUsage(
            input_tokens=12345, output_tokens=1087, reasoning_tokens=1020,
        )

        assert (
            "Tokens: 12,345 in / 1,087 out (1,020 reasoning)"
            in self._output(usage)
        )

    def test_no_parenthetical_without_reasoning(self):
        output = self._output(TokenUsage(input_tokens=10, output_tokens=5))

        assert "Tokens: 10 in / 5 out" in output
        assert "reasoning" not in output

    def test_no_tokens_line_without_usage(self):
        output = self._output(None)

        assert "Tokens:" not in output
        assert "Done — completed" in output


# ============================================================================
# The tools panel's columns
# ============================================================================


class TestToolTableColumns:
    """What a tool call shows in the streaming panel: its own arguments in
    the Tool column, its own line about the call in Description."""

    @staticmethod
    def _renderer() -> StreamingRenderer:
        return StreamingRenderer(console=Console(file=io.StringIO()))

    @staticmethod
    def _panel_text(renderer: StreamingRenderer) -> str:
        """The whole renderable, rendered wide enough that a cell cannot
        wrap mid-phrase and hide the string under test."""
        buf = io.StringIO()
        Console(file=buf, width=200).print(renderer._build_renderable())
        return buf.getvalue()

    def test_the_description_gets_a_column_of_its_own(self):
        renderer = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="shell",
            input={
                "description": "count the python files",
                "command": "ls -la",
            },
        ))

        text = self._panel_text(renderer)

        assert "Description" in text              # the column the call fills
        assert "count the python files" in text
        assert "shell(ls -la)" in text            # the command alone
        assert "description=" not in text         # never one of the arguments

    def test_a_multi_line_command_reads_on_one_line(self):
        renderer = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="shell",
            input={"command": "ls -la\ngit status --short"},
        ))

        assert "shell(ls -la git status --short)" in self._panel_text(renderer)

    def test_the_end_event_parameters_are_what_the_row_keeps(self):
        """The end event carries the authoritative input, so the row is
        rebuilt from it — whatever the start had said."""
        renderer = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1", tool_name="shell", input={"command": "ls"},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="t1",
            tool_name="shell",
            input={"command": "ls -la", "description": "list the files"},
            result=ToolResult(
                tool_id="t1", tool_name="shell", success=True, output="ok",
            ),
        ))

        text = self._panel_text(renderer)

        assert "shell(ls -la)" in text
        assert "list the files" in text


class TestOneShotToolLine:
    """One-shot mode prints prose, not a table — so the description rides
    along after the call rather than into a column."""

    @staticmethod
    def _renderer() -> tuple[NonStreamingRenderer, io.StringIO]:
        buf = io.StringIO()
        return NonStreamingRenderer(console=Console(file=buf)), buf

    def test_the_line_carries_the_description(self):
        renderer, buf = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="shell",
            input={
                "description": "list the files",
                "command": "ls -la",
            },
        ))

        assert "▶ shell(ls -la) — list the files" in buf.getvalue()

    def test_a_call_without_a_description_reads_as_before(self):
        renderer, buf = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="read_file",
            input={"file_path": "README.md"},
        ))

        assert "▶ read_file(file_path='README.md')" in buf.getvalue()


# ============================================================================
# Hidden tool calls
# ============================================================================


class TestHiddenToolCallInPanel:
    """A tool that declares itself invisible (``todo_write``) draws no row:
    its list is already on screen as todo updates, so a row would say the
    same thing twice."""

    @staticmethod
    def _renderer() -> StreamingRenderer:
        return StreamingRenderer(console=Console(file=io.StringIO()))

    @staticmethod
    def _panel_text(renderer: StreamingRenderer) -> str:
        buf = io.StringIO()
        Console(file=buf, width=200).print(renderer._build_renderable())
        return buf.getvalue()

    def test_the_start_draws_no_row(self):
        renderer = self._renderer()
        renderer.on_tool_use_start(ToolUseStart(
            tool_id="t1",
            tool_name="todo_write",
        ))

        assert renderer._tools == {}
        assert "todo_write" not in self._panel_text(renderer)

    def test_the_rest_of_the_call_leaves_no_trace_either(self):
        """The id is what keeps the rest of a turned-away call from drawing
        a row: both starts are filtered before either can create one."""
        renderer = self._renderer()
        renderer.on_tool_use_start(ToolUseStart(
            tool_id="t1",
            tool_name="todo_write",
        ))

        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="todo_write",
            input={"todos": []},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="t1",
            tool_name="todo_write",
            input={"todos": []},
            result=ToolResult(
                tool_id="t1", tool_name="todo_write", success=True,
                output="Todo list cleared.",
            ),
        ))

        assert renderer._tools == {}
        text = self._panel_text(renderer)
        assert "todo_write" not in text
        assert "cleared" not in text

    def test_a_tool_that_declares_nothing_still_draws_its_row(self):
        """The filter is the declaration's, not every tool's."""
        renderer = self._renderer()
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1", tool_name="shell", input={"command": "ls"},
        ))

        assert renderer._tools["t1"].name == "shell"


class TestOneShotHiddenToolCall:
    """One-shot mode prints prose per call, and prints none for a hidden
    one — the todo lines are the whole story there too."""

    def test_a_hidden_call_prints_nothing(self):
        buf = io.StringIO()
        renderer = NonStreamingRenderer(console=Console(file=buf))
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="t1",
            tool_name="todo_write",
            input={"todos": []},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="t1",
            tool_name="todo_write",
            input={"todos": []},
            result=ToolResult(
                tool_id="t1", tool_name="todo_write", success=True,
                output="Todo list cleared.",
            ),
        ))

        assert buf.getvalue() == ""


class TestPausePromptLine:
    """The gate's line: one line, whatever the call put in it.

    The prompt lands in a Panel *title* — the one slot that cannot hold a
    second line and that Rich parses for markup — and it now carries the
    call's arguments as written, so a shell command with brackets in it
    goes through there.  Both halves are pinned: the flattening, and that
    the brackets survive it.
    """

    def _display(self) -> ToolCallPrompt:
        return ToolCallPrompt(
            title="Allow this tool call?",
            content="set -e\n[ -f notes.md ] && rm notes.md[/]",
            description="drop the notes",
        )

    def test_the_one_shot_renderer_prints_the_line(self):
        buf = io.StringIO()
        renderer = NonStreamingRenderer(console=Console(file=buf, width=120))
        renderer.on_agent_paused(AgentPaused(prompt=self._display()))

        out = buf.getvalue()
        assert "Allow this tool call? — set -e [ -f notes.md ] && rm notes.md[/]" in out
        assert "[Approve/Deny/Tell Toddler what to do instead]" in out

    def test_the_streaming_title_keeps_the_brackets(self):
        """A title is markup: passed as a plain string, ``[ -f x ]`` reads
        as a tag and vanishes, and ``[/]`` closes one that was never
        opened — which raises, mid-turn, at the gate."""
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        renderer._confirm_prompt = self._display().one_line()

        buf = io.StringIO()
        Console(file=buf, width=120).print(renderer._build_confirm_table())

        assert "[ -f notes.md ] && rm notes.md[/]" in buf.getvalue()


# ---------------------------------------------------------------------------
# ask_user answers
# ---------------------------------------------------------------------------


def _question() -> Question:
    return Question(
        title="Storage",
        content="Where should sessions live?",
        choices=(
            QuestionChoice("SQLite", "One file, no server."),
            QuestionChoice("Redis", "Shared storage."),
        ),
    )


class TestQuestionConfirmation:
    """The non-streaming confirm path carries the chosen index and can
    be declined, which is what the CLI's sequential ask uses."""

    def _renderer(self) -> tuple[NonStreamingRenderer, io.StringIO]:
        buf = io.StringIO()
        return NonStreamingRenderer(console=Console(file=buf)), buf

    def test_confirm_result_defaults(self):
        result = ConfirmResult(decision="deny")
        assert result.choice_index is None
        assert result.cancelled is False

    async def test_numbered_choice_returns_its_index_and_shows_the_detail(
        self,
    ):
        renderer, buf = self._renderer()
        renderer._console.input = lambda prompt="": "2"

        result = await renderer.confirm(
            "Question 1/2: Storage",
            ["1. SQLite — One file.", "2. Redis — Shared."],
            detail="Where should sessions live?",
        )

        assert result.choice_index == 1
        assert result.cancelled is False
        # Arbitrary labels have no approve/deny meaning; the index is the
        # answer the ask path reads.
        assert result.decision == "deny"
        output = buf.getvalue()
        assert "Where should sessions live?" in output
        assert "SQLite" in output

    async def test_eof_is_cancelled(self):
        renderer, _ = self._renderer()

        def raise_eof(prompt=""):
            raise EOFError

        renderer._console.input = raise_eof
        result = await renderer.confirm("Q", ["1. A", "2. B"])
        assert result.cancelled is True
        assert result.choice_index is None

    async def test_unmatched_input_is_cancelled(self):
        renderer, _ = self._renderer()
        renderer._console.input = lambda prompt="": "nope"

        result = await renderer.confirm("Q", ["1. A", "2. B"])
        assert result.cancelled is True
        assert result.choice_index is None

    def test_case_is_preserved_in_the_choice_labels(self):
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        renderer._confirm_choices = ["1. SQLite — one file"]
        renderer._confirm_prompt = "Storage"
        buf = io.StringIO()
        Console(file=buf, width=120).print(renderer._build_confirm_table())
        assert "SQLite" in buf.getvalue()
        assert "Sqlite" not in buf.getvalue()

    async def test_the_feedback_row_takes_free_text_by_index(self):
        renderer, _ = self._renderer()
        inputs = iter(["3", "use the other file"])
        renderer._console.input = lambda prompt="": next(inputs)

        result = await renderer.confirm(
            "Allow this tool call?",
            ["Approve", "Deny", "Tell me why"],
            feedback_index=2,
        )

        assert result.decision == "feedback"
        assert result.feedback == "use the other file"
        assert result.choice_index == 2

    async def test_the_input_label_names_the_free_text_prompt(self):
        renderer, _ = self._renderer()
        prompts: list[str] = []
        inputs = iter(["3", "DuckDB"])

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(inputs)

        renderer._console.input = fake_input
        result = await renderer.confirm(
            "Question 1/2: Storage",
            ["1. SQLite — One file.", "2. Redis — Shared.", "3. Other"],
            feedback_index=2,
            input_label="Your answer",
        )

        assert prompts == ["> ", "Your answer: "]
        assert result.feedback == "DuckDB"
        assert result.choice_index == 2

    def test_a_feedback_row_uses_its_own_title(self):
        """The tool words the row; the renderer only knows its index."""
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        renderer._confirm_choices = ["Allow", "Tell me why"]
        renderer._confirm_feedback_index = 1
        renderer._confirm_prompt = "Gate"
        buf = io.StringIO()
        Console(file=buf, width=120).print(renderer._build_confirm_table())
        assert "Tell me why" in buf.getvalue()


class TestQuestionToolRow:
    """The visible ask_user call is a normal tools-panel row: the signature
    counts the questions, the result names what was chosen."""

    def test_the_row_names_the_question_count(self):
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="q1",
            tool_name="ask_user",
            input={"questions": [_question().to_dict()]},
        ))
        row = renderer._tools["q1"]
        assert row.signature == "ask_user(1 question)"
        assert row.status == "running"

    def test_the_row_lists_the_chosen_titles(self):
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        raw = [_question().to_dict()]
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
            result=ToolResult(
                tool_id="q1", tool_name="ask_user", success=True,
                output="The user answered your questions:",
                metadata={
                    "questions": raw,
                    "answers": [{"choice": 1, "content": None}],
                },
            ),
        ))
        row = renderer._tools["q1"]
        assert row.result == "Redis"
        assert row.status == "success"

    def test_a_free_text_answer_names_the_users_words(self):
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        raw = [_question().to_dict()]
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
            result=ToolResult(
                tool_id="q1", tool_name="ask_user", success=True,
                output="The user answered your questions:",
                metadata={
                    "questions": raw,
                    "answers": [{"choice": 1, "content": "DuckDB"}],
                },
            ),
        ))
        assert renderer._tools["q1"].result == "DuckDB"

    def test_an_old_integer_answer_still_replays(self):
        """Sessions written before answers became objects hold a bare
        index; the row still names what was chosen."""
        renderer = StreamingRenderer(console=Console(file=io.StringIO()))
        raw = [_question().to_dict()]
        renderer.on_tool_call_start(ToolCallStart(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
        ))
        renderer.on_tool_call_end(ToolCallEnd(
            tool_id="q1", tool_name="ask_user",
            input={"questions": raw},
            result=ToolResult(
                tool_id="q1", tool_name="ask_user", success=True,
                output="The user answered your questions:",
                metadata={"questions": raw, "answers": [1]},
            ),
        ))
        assert renderer._tools["q1"].result == "Redis"
