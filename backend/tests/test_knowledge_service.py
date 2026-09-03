"""Pure/offline tests for the hybrid retrieval ordering contract."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services.knowledge_service import KnowledgeService


def _doc(doc_id: str, content: str, **scores) -> dict:
    return {
        "id": doc_id,
        "content": content,
        "title": f"doc-{doc_id}",
        "source_type": "uploaded_doc",
        "source_id": doc_id,
        "metadata": {"document_id": doc_id},
        **scores,
    }


def test_rrf_merges_scores_from_both_retrievers() -> None:
    service = KnowledgeService()
    fused = service._rrf_fusion(
        [_doc("a", "alpha", vector_score=0.9), _doc("b", "beta")],
        [_doc("a", "alpha", fulltext_score=0.8), _doc("c", "gamma")],
    )

    assert fused[0]["id"] == "a"
    assert fused[0]["vector_score"] == 0.9
    assert fused[0]["fulltext_score"] == 0.8
    assert fused[0]["score"] > fused[1]["score"]


def test_chinese_query_terms_are_not_dependent_on_spaces() -> None:
    terms = KnowledgeService._query_terms("为什么选择向量数据库？")
    assert "向量" in terms
    assert "数据" in terms


@pytest.mark.asyncio
async def test_no_embedding_fallback_still_returns_scored_deduped_top_k() -> None:
    service = KnowledgeService()
    lexical_candidates = [
        _doc("a", "项目最终选择向量数据库"),
        _doc("b", "项目最终选择向量数据库"),  # duplicate overlap chunk
        _doc("c", "向量检索需要 embedding"),
        _doc("d", "无关内容"),
    ]

    with patch(
        "app.services.knowledge_service.embedding_service.embed_text",
        new=AsyncMock(return_value=None),
    ), patch.object(
        service,
        "_fulltext_search",
        new=AsyncMock(return_value=lexical_candidates),
    ):
        results = await service.search(object(), "为什么选择向量数据库", top_k=2)

    assert len(results) == 2
    assert {item["id"] for item in results} != {"a", "b"}
    assert all(isinstance(item["score"], float) for item in results)
    assert results[0]["score"] == 1.0


def test_rerank_does_not_mutate_retriever_results() -> None:
    service = KnowledgeService()
    original = [_doc("a", "SSE stream", score=0.1)]
    reranked = service._rerank("SSE", original)

    assert "rerank_score" not in original[0]
    assert reranked[0]["rerank_score"] == 1.0


@pytest.mark.asyncio
async def test_embedding_count_mismatch_does_not_drop_chunks() -> None:
    service = KnowledgeService()
    db = type(
        "FakeDB",
        (),
        {
            "add": Mock(),
            "flush": AsyncMock(),
            "refresh": AsyncMock(),
        },
    )()
    def fake_chunks(_content, base_metadata):
        return [
            {
                "content": "first",
                "metadata": {**base_metadata, "chunk_index": 0},
            },
            {
                "content": "second",
                "metadata": {**base_metadata, "chunk_index": 1},
            },
        ]

    with patch(
        "app.services.knowledge_service.document_chunker.split_with_metadata",
        side_effect=fake_chunks,
    ), patch(
        "app.services.knowledge_service.embedding_service.embed_batch",
        new=AsyncMock(return_value=[[0.1] * 1024]),
    ):
        documents = await service.index_text(db, "title", "content")

    assert len(documents) == 2
    assert documents[1].embedding is None
    assert documents[0].source_id == documents[1].source_id
    assert documents[0].metadata_["document_id"] == str(documents[0].source_id)
