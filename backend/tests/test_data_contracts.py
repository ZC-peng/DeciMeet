"""Offline regression tests for migrations and persistence contracts."""

from pathlib import Path
from unittest.mock import AsyncMock

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.models.action_item import ActionItem
from app.models.decision import Decision, DecisionOption, DecisionRelation
from app.models.knowledge_doc import KnowledgeDocument
from app.models.meeting import Meeting
from app.models.risk import Risk
from app.models.summary import Summary
from app.models.transcript import Transcript
from app.services.decision_graph_service import DecisionGraphService
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_alembic_chain_has_one_expected_head() -> None:
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_bases() == ["49d34cdead0c"]
    assert script.get_heads() == ["e5f6a7b8c9d0"]
    assert len(list(script.walk_revisions())) == 9


def test_clean_install_enables_vector_before_vector_columns() -> None:
    initial = (
        BACKEND_DIR
        / "alembic"
        / "versions"
        / "2026_06_24_1656-49d34cdead0c_initial_schema.py"
    ).read_text(encoding="utf-8")

    assert initial.index("CREATE EXTENSION IF NOT EXISTS vector") < initial.index(
        "VECTOR(dim=1024)"
    )


def test_repair_migration_contains_model_drift_and_integrity_guards() -> None:
    migration = (
        BACKEND_DIR
        / "alembic"
        / "versions"
        / "2026_09_02_1200-d4e5f6a7b8c9_runtime_data_consistency.py"
    ).read_text(encoding="utf-8")

    for contract in (
        "transcription_mode",
        "ADD COLUMN IF NOT EXISTS evidence JSONB",
        "uq_summaries_meeting_id",
        "uq_transcripts_meeting_seq",
        "ix_knowledge_documents_content_fts",
        "ix_knowledge_documents_embedding_hnsw",
    ):
        assert contract in migration


def test_orm_constraints_match_repaired_schema() -> None:
    summary_constraints = {c.name for c in Summary.__table__.constraints}
    transcript_constraints = {c.name for c in Transcript.__table__.constraints}

    assert "uq_summaries_meeting_id" in summary_constraints
    assert "uq_transcripts_meeting_seq" in transcript_constraints
    assert "transcription_mode" in Meeting.__table__.columns
    assert "evidence" in Decision.__table__.columns


def test_orm_index_metadata_matches_migrated_postgres_schema() -> None:
    expected_sql = {
        "ix_action_items_meeting_id": (
            "CREATE INDEX ix_action_items_meeting_id ON action_items (meeting_id)"
        ),
        "idx_decisions_embedding": (
            "CREATE INDEX idx_decisions_embedding ON decisions USING ivfflat "
            "(embedding vector_cosine_ops) WITH (lists = 100)"
        ),
        "idx_decisions_meeting": (
            "CREATE INDEX idx_decisions_meeting ON decisions (meeting_id)"
        ),
        "idx_decision_options_decision": (
            "CREATE INDEX idx_decision_options_decision ON decision_options "
            "(decision_id)"
        ),
        "idx_decision_relations_source": (
            "CREATE INDEX idx_decision_relations_source ON decision_relations "
            "(source_decision_id)"
        ),
        "ix_decision_relations_target": (
            "CREATE INDEX ix_decision_relations_target ON decision_relations "
            "(target_decision_id)"
        ),
        "ix_knowledge_documents_content_fts": (
            "CREATE INDEX ix_knowledge_documents_content_fts ON "
            "knowledge_documents USING gin (to_tsvector('simple', content))"
        ),
        "ix_knowledge_documents_embedding_hnsw": (
            "CREATE INDEX ix_knowledge_documents_embedding_hnsw ON "
            "knowledge_documents USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        ),
        "ix_knowledge_documents_source": (
            "CREATE INDEX ix_knowledge_documents_source ON knowledge_documents "
            "(source_type, source_id)"
        ),
        "ix_risks_meeting_id": (
            "CREATE INDEX ix_risks_meeting_id ON risks (meeting_id)"
        ),
    }
    tables = (
        ActionItem.__table__,
        Decision.__table__,
        DecisionOption.__table__,
        DecisionRelation.__table__,
        KnowledgeDocument.__table__,
        Risk.__table__,
    )
    actual_sql = {
        index.name: " ".join(
            str(CreateIndex(index).compile(dialect=postgresql.dialect())).split()
        )
        for table in tables
        for index in table.indexes
        if index.name in expected_sql
    }

    assert actual_sql == {
        name: " ".join(statement.split())
        for name, statement in expected_sql.items()
    }


def test_decision_payload_preserves_traceable_agent_fields() -> None:
    payload = DecisionGraphService._normalize_decision_payload(
        {
            "title": "选择 SSE 推送",
            "context": "需要单向流式返回",
            "snippet": "最终决定使用 SSE。",
            "chosen_option": "SSE",
            "confidence": "0.91",
            "decided_at": "2026-09-02T10:00:00Z",
            "options": [{"name": "SSE", "pros": ["简单"], "cons": []}],
        }
    )

    assert payload is not None
    assert payload["chosen"] == "SSE"
    assert payload["confidence"] == 0.91
    assert payload["decided_at"].utcoffset() is not None
    assert payload["evidence"] == [
        {"type": "transcript_snippet", "text": "最终决定使用 SSE。"}
    ]


def test_invalid_decision_cannot_replace_history() -> None:
    assert DecisionGraphService._normalize_decision_payload({"title": "  "}) is None


@pytest.mark.asyncio
async def test_invalid_agent_batch_is_rejected_before_history_delete() -> None:
    db = type("FakeDB", (), {"execute": AsyncMock()})()
    with pytest.raises(ValueError, match="拒绝覆盖历史决策"):
        await DecisionGraphService().save_decisions(
            db, __import__("uuid").uuid4(), [{"title": ""}]
        )
    db.execute.assert_not_awaited()
