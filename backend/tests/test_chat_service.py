"""Chat RAG route merging and SSE event contract tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat_service import ChatService
from app.services.decision_graph_service import decision_graph_service
from app.services.knowledge_service import knowledge_service


def test_decision_intent_boosts_decision_route_without_mutating_inputs() -> None:
    service = ChatService()
    docs = [{"id": "d1", "title": "纪要", "score": 1.0}]
    decisions = [
        {"id": "x1", "title": "数据库选型", "score": 0.78, "source_type": "decision"}
    ]

    result = service._merge_retrieval_routes(
        "数据库为什么选择这个方案？", docs, decisions, top_k=2
    )

    assert result[0]["retrieval_route"] == "decision"
    assert result[1]["retrieval_route"] == "document"
    assert "retrieval_score" not in docs[0]
    assert "retrieval_score" not in decisions[0]


@pytest.mark.asyncio
async def test_unconfigured_llm_emits_terminal_error_event() -> None:
    service = ChatService()
    service.client = None

    events = [event async for event in service.chat_stream(None, "session", "问题")]

    assert events == [{"type": "error", "message": "LLM 未配置"}]


class _FakeStream:
    def __aiter__(self):
        self._done = False
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="答案"))]
        )


@pytest.mark.asyncio
async def test_successful_stream_ends_with_sources() -> None:
    service = ChatService()
    completions = SimpleNamespace(create=AsyncMock(return_value=_FakeStream()))
    service.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    service.save_message = AsyncMock()
    service.get_session_messages = AsyncMock(
        return_value=[SimpleNamespace(role="user", content="数据库方案")]
    )

    with patch.object(
        knowledge_service,
        "search",
        AsyncMock(
            return_value=[
                {
                    "id": "doc-1",
                    "title": "架构评审纪要",
                    "content": "采用 PostgreSQL",
                    "source_type": "meeting_summary",
                    "score": 1.0,
                }
            ]
        ),
    ), patch.object(
        decision_graph_service,
        "search",
        AsyncMock(return_value=[]),
    ):
        events = [
            event
            async for event in service.chat_stream(None, "session", "数据库方案")
        ]

    assert events[0] == {"type": "token", "content": "答案"}
    assert events[-1]["type"] == "done"
    assert events[-1]["sources"][0]["title"] == "架构评审纪要"
    assert service.save_message.await_count == 2
