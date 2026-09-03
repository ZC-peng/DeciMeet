"""Summary persistence/AgentRun consistency tests without a live LLM or DB."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.summary import Summary
from app.services.summary_service import SummaryService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preserve_previous_success", "previous_content", "expected_content"),
    [
        (True, "previous good snapshot", "previous good snapshot"),
        (False, "⚠️ stale failure", "⚠️ summary_agent: 返回空纪要"),
    ],
)
async def test_empty_agent_output_keeps_only_a_previous_successful_snapshot(
    preserve_previous_success: bool,
    previous_content: str,
    expected_content: str,
) -> None:
    events = []

    async def record_commit():
        events.append("summary_commit")

    async def record_finish(*_args, **_kwargs):
        events.append("run_finish")

    db = SimpleNamespace(
        execute=AsyncMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        commit=AsyncMock(side_effect=record_commit),
    )
    summary = Summary(
        meeting_id=uuid.uuid4(), content=previous_content, status="generating"
    )
    fake_graph = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value={
                "summary": "",
                "key_points": [],
                "action_items": [],
                "risks": [],
                "errors": [],
                "paused": False,
            }
        )
    )
    fake_run_service = SimpleNamespace(
        record_step_start=AsyncMock(),
        record_step_end=AsyncMock(),
        finish_run=AsyncMock(side_effect=record_finish),
    )

    with patch(
        "app.agents.meeting_graph_v2.meeting_graph_v2", fake_graph
    ), patch(
        "app.services.agent_run_service.agent_run_service", fake_run_service
    ):
        result = await SummaryService()._run_v2_workflow(
            db=db,
            meeting_id=summary.meeting_id,
            meeting=SimpleNamespace(title="meeting", start_time=None),
            transcript_text="transcript",
            summary=summary,
            agent_run=SimpleNamespace(id="run-id"),
            budget_guard=None,
            preserve_previous_success=preserve_previous_success,
        )

    assert result.status == "failed"
    assert result.content == expected_content
    assert events == ["summary_commit", "run_finish"]
    assert fake_run_service.finish_run.await_args.args[1] == "failed"
