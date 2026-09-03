"""评审决策知识库模型

三表结构：
- decisions: 决策主表（含 pgvector 向量列）
- decision_options: 决策候选方案
- decision_relations: 决策间关系（写入时即时向量关联）
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    DateTime,
    Float,
    Boolean,
    ForeignKey,
    Index,
    func,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.db.base import Base


class Decision(Base):
    """决策主表：评审会议中识别出的明确决策

    每个 Decision 关联一个 Meeting，含 title/context/snippet/chosen_option
    以及向量索引（title + context 向量化，用于跨会议检索）
    """

    __tablename__ = "decisions"
    __table_args__ = (
        Index("idx_decisions_meeting", "meeting_id"),
        Index(
            "idx_decisions_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(50), nullable=False)
    context: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)  # 决策段原文
    chosen_option: Mapped[str | None] = mapped_column(String(30))
    reasons: Mapped[list | None] = mapped_column(JSONB)  # ["理由1", "理由2"]
    objections: Mapped[list | None] = mapped_column(JSONB)  # [{"from": "张三", "content": "反对意见"}]
    decided_by: Mapped[list | None] = mapped_column(JSONB)  # ["张三", "李四"]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float | None] = mapped_column(Float)  # DecisionDetector 置信度
    # 支撑结论的可追溯证据。允许保存原文片段、时间戳或 chunk 引用等结构。
    evidence: Mapped[list | dict | None] = mapped_column(JSONB)
    embedding = mapped_column(Vector(1024))  # title + context 向量化
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    options: Mapped[list["DecisionOption"]] = relationship(
        back_populates="decision", cascade="all, delete-orphan"
    )


class DecisionOption(Base):
    """决策候选方案：每个决策 1~5 个方案"""

    __tablename__ = "decision_options"
    __table_args__ = (
        Index("idx_decision_options_decision", "decision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(30), nullable=False)
    pros: Mapped[list | None] = mapped_column(JSONB)
    cons: Mapped[list | None] = mapped_column(JSONB)
    proposed_by: Mapped[str | None] = mapped_column(String(50))
    is_chosen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decision: Mapped["Decision"] = relationship(back_populates="options")


class DecisionRelation(Base):
    """决策间关系：写入时即时向量关联 top-3 相似历史决策

    当前 relation_type 统一为 'relates'；后续可细化为
    supersedes / contradicts / evolves。
    """

    __tablename__ = "decision_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_decision_id",
            "target_decision_id",
            name="uq_decision_relation",
        ),
        Index("idx_decision_relations_source", "source_decision_id"),
        Index("ix_decision_relations_target", "target_decision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(20), default="relates")
    context: Mapped[str | None] = mapped_column(Text)
    similarity_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
