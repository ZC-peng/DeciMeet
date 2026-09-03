"""决策图谱服务：写入 + 向量关联 + 检索

决策单独建表并维护向量索引；写入时关联 top-3 相似历史决策，
relation_type 当前统一为 'relates'。
"""

import uuid
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import Decision, DecisionOption, DecisionRelation
from app.services.embedding_service import embedding_service

logger = logging.getLogger(__name__)


class DecisionGraphService:
    """决策图谱服务"""

    @staticmethod
    def _as_dict(value: Any) -> dict:
        """Accept plain dicts and Pydantic outputs without coupling this layer."""
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(by_alias=True)
        return {}

    @classmethod
    def _normalize_decision_payload(cls, raw: Any) -> dict | None:
        """Normalize the Agent contract before touching existing database rows.

        The detector/extractor pipeline has evolved over time, so persistence
        accepts both ``chosen`` and ``chosen_option`` and preserves traceable
        ``snippet``/``evidence`` fields when they are present.
        """
        data = cls._as_dict(raw)
        title = str(data.get("title") or "").strip()
        if not title:
            return None

        context = str(data.get("context") or "").strip() or None
        snippet = str(data.get("snippet") or "").strip() or None
        chosen = str(
            data.get("chosen") or data.get("chosen_option") or ""
        ).strip() or None

        options: list[dict] = []
        for raw_option in data.get("options") or []:
            option = cls._as_dict(raw_option)
            name = str(option.get("name") or "").strip()
            if not name:
                continue
            options.append(
                {
                    "name": name[:30],
                    "pros": option.get("pros") or [],
                    "cons": option.get("cons") or [],
                    "proposed_by": (
                        str(option.get("proposed_by"))[:50]
                        if option.get("proposed_by")
                        else None
                    ),
                    "is_chosen": bool(option.get("is_chosen", False)),
                }
            )

        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
            if confidence is not None and not 0.0 <= confidence <= 1.0:
                logger.warning(
                    "[DecisionGraph] 忽略越界 confidence=%r", confidence
                )
                confidence = None

        decided_at = data.get("decided_at")
        if isinstance(decided_at, str):
            try:
                decided_at = datetime.fromisoformat(
                    decided_at.replace("Z", "+00:00")
                )
            except ValueError:
                decided_at = None
        elif decided_at is not None and not isinstance(decided_at, datetime):
            decided_at = None

        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [{"type": "transcript_snippet", "text": evidence}]
        elif evidence is not None and not isinstance(evidence, (list, dict)):
            evidence = None
        # A detector snippet is itself evidence.  Store it in a structured form
        # when the upstream node did not provide a richer evidence object.
        if evidence is None and snippet:
            evidence = [{"type": "transcript_snippet", "text": snippet}]

        return {
            "title": title[:50],
            "context": context,
            "snippet": snippet,
            "chosen": chosen[:30] if chosen else None,
            "reasons": data.get("reasons") or [],
            "objections": data.get("objections") or [],
            "decided_by": data.get("decided_by") or [],
            "decided_at": decided_at,
            "confidence": confidence,
            "evidence": evidence,
            "options": options,
        }

    async def save_decisions(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID,
        decisions: list[dict],
    ) -> list[Decision]:
        """批量保存决策（含 options + 即时向量关联）

        流程：
            1. 删除该 meeting 的旧决策（cascade 会删 options + relations）
            2. 对每个决策生成 embedding（title + context）
            3. 写 decisions + decision_options
            4. 对每个新决策检索 top-3 相似历史决策，写 decision_relations

        Args:
            db: 数据库会话
            meeting_id: 关联的会议 ID
            decisions: decision_extractor 节点输出的决策列表

        Returns:
            已保存的 Decision ORM 对象列表
        """
        normalized = [
            item
            for raw in decisions
            if (item := self._normalize_decision_payload(raw)) is not None
        ]
        if decisions and not normalized:
            raise ValueError("Agent 返回的决策均缺少有效 title，拒绝覆盖历史决策")

        # Embedding is remote I/O.  Finish it before deleting the previous
        # successful snapshot so a provider failure cannot create a half-written
        # replacement.
        prepared: list[tuple[dict, list[float] | None]] = []
        for item in normalized:
            embed_text = f"{item['title']} {item.get('context') or ''}".strip()
            prepared.append((item, await embedding_service.embed_text(embed_text)))

        # 1. 删除旧决策（cascade 会删 options 和 relations）
        await db.execute(
            delete(Decision).where(Decision.meeting_id == meeting_id)
        )
        await db.flush()

        saved: list[Decision] = []
        for d, embedding in prepared:
            # 3. 写 Decision 主表
            decision = Decision(
                meeting_id=meeting_id,
                title=d["title"],
                context=d.get("context"),
                snippet=d.get("snippet"),
                chosen_option=d.get("chosen"),
                reasons=d.get("reasons"),
                objections=d.get("objections"),
                decided_by=d.get("decided_by"),
                decided_at=d.get("decided_at"),
                confidence=d.get("confidence"),
                evidence=d.get("evidence"),
                embedding=embedding,
            )
            db.add(decision)
            await db.flush()  # 拿到 id

            # 4. 写 Options 方案表
            for opt in d.get("options", []):
                option = DecisionOption(
                    decision_id=decision.id,
                    name=opt.get("name", ""),
                    pros=opt.get("pros"),
                    cons=opt.get("cons"),
                    proposed_by=opt.get("proposed_by"),
                    is_chosen=(
                        opt.get("name") == d.get("chosen")
                        or bool(opt.get("is_chosen"))
                    ),
                )
                db.add(option)

            # 5. 即时关联 top-3 相似历史决策
            if embedding:
                await self._link_similar(
                    db, decision.id, meeting_id, embedding
                )

            saved.append(decision)

        await db.flush()
        logger.info(
            f"[DecisionGraph] 保存 {len(saved)} 个决策，meeting={meeting_id}"
        )
        return saved

    async def _link_similar(
        self,
        db: AsyncSession,
        decision_id: uuid.UUID,
        meeting_id: uuid.UUID,
        embedding: list[float],
        top_k: int = 3,
        threshold: float = 0.7,
    ) -> None:
        """检索 top-3 相似历史决策，写入 decision_relations（双向）

        当前 relation_type 统一为 'relates'，后续可细化为
        supersedes / contradicts / evolves。

        双向关联：写入 source→target 和 target→source 两条记录
        避免重复：检查 UniqueConstraint (source_decision_id, target_decision_id)

        Args:
            decision_id: 当前决策 ID（排除自身）
            meeting_id: 当前会议 ID（只关联其他会议的历史决策）
            embedding: 当前决策的向量
            top_k: 检索数量
            threshold: 相似度阈值（cosine_similarity = 1 - cosine_distance）
        """
        try:
            # A database error inside a caught exception would otherwise leave
            # PostgreSQL's outer transaction aborted.  A savepoint contains this
            # optional enrichment step.
            async with db.begin_nested():
                stmt = (
                    select(
                        Decision.id,
                        Decision.embedding.cosine_distance(embedding).label("distance"),
                    )
                    .where(
                        Decision.id != decision_id,
                        Decision.meeting_id != meeting_id,
                        Decision.embedding.is_not(None),
                    )
                    .order_by("distance")
                    .limit(top_k)
                )
                result = await db.execute(stmt)

                # 先查询已存在的关系，避免插入时 UniqueConstraint 冲突
                existing_stmt = select(
                    DecisionRelation.source_decision_id,
                    DecisionRelation.target_decision_id,
                ).where(
                    (DecisionRelation.source_decision_id == decision_id)
                    | (DecisionRelation.target_decision_id == decision_id)
                )
                existing_result = await db.execute(existing_stmt)
                existing_pairs = {
                    (str(s), str(t)) for s, t in existing_result.all()
                }

                for related_id, distance in result.all():
                    if distance is None:
                        continue
                    # cosine_distance ∈ [0, 2]，相似度 = 1 - distance ∈ [-1, 1]
                    similarity = max(0.0, 1.0 - float(distance))
                    if similarity < threshold:
                        continue

                    # 双向关联：决策 A → B
                    pair_ab = (str(decision_id), str(related_id))
                    if pair_ab not in existing_pairs:
                        relation = DecisionRelation(
                            source_decision_id=decision_id,
                            target_decision_id=related_id,
                            relation_type="relates",
                            context=f"向量相似度 {similarity:.2f}",
                            similarity_score=similarity,
                        )
                        db.add(relation)
                        existing_pairs.add(pair_ab)

                    # 双向关联：决策 B → A（相同相似度）
                    pair_ba = (str(related_id), str(decision_id))
                    if pair_ba not in existing_pairs:
                        reverse_relation = DecisionRelation(
                            source_decision_id=related_id,
                            target_decision_id=decision_id,
                            relation_type="relates",
                            context=f"向量相似度 {similarity:.2f}",
                            similarity_score=similarity,
                        )
                        db.add(reverse_relation)
                        existing_pairs.add(pair_ba)
                await db.flush()
        except Exception as e:
            logger.warning(f"[DecisionGraph] 关联失败（不影响决策保存）: {e}")

    async def search(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
    ) -> list[dict]:
        """决策语义检索（供 AI 对话 RAG 召回）

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            决策列表，每项含 id / title / context / chosen_option / meeting_id / score
        """
        query = query.strip()
        if not query:
            return []
        top_k = max(1, min(int(top_k), 50))
        embedding = await embedding_service.embed_text(query)
        if not embedding:
            logger.warning("[DecisionGraph] 查询向量化失败，返回空列表")
            return []

        try:
            stmt = (
                select(
                    Decision,
                    Decision.embedding.cosine_distance(embedding).label("distance"),
                )
                .where(Decision.embedding.is_not(None))
                .order_by("distance")
                .limit(top_k)
            )
            async with db.begin_nested():
                result = await db.execute(stmt)
                rows = result.all()
            return [
                {
                    "id": str(d.id),
                    "title": d.title,
                    "context": d.context,
                    "chosen_option": d.chosen_option,
                    "meeting_id": str(d.meeting_id) if d.meeting_id else None,
                    "score": max(0.0, 1.0 - float(distance)),
                    "source_type": "decision",
                }
                for d, distance in rows
            ]
        except Exception as e:
            logger.error(f"[DecisionGraph] 检索失败: {e}")
            return []

    async def list_decisions(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Decision], int]:
        """决策列表（含分页 + 按 meeting 筛选）"""
        stmt = select(Decision).order_by(Decision.created_at.desc())
        if meeting_id:
            stmt = stmt.where(Decision.meeting_id == meeting_id)
        stmt = stmt.offset(skip).limit(limit)
        result = await db.execute(stmt)
        decisions = list(result.scalars().all())

        count_stmt = select(func.count(Decision.id))
        if meeting_id:
            count_stmt = count_stmt.where(Decision.meeting_id == meeting_id)
        total = (await db.execute(count_stmt)).scalar_one()
        return decisions, total

    async def get_decision(
        self, db: AsyncSession, decision_id: uuid.UUID
    ) -> dict | None:
        """决策详情（含 options + 关联决策）"""
        result = await db.execute(
            select(Decision).where(Decision.id == decision_id)
        )
        decision = result.scalar_one_or_none()
        if not decision:
            return None

        # options
        opts_result = await db.execute(
            select(DecisionOption)
            .where(DecisionOption.decision_id == decision_id)
            .order_by(DecisionOption.created_at)
        )
        options = list(opts_result.scalars().all())

        # 关联决策（双向查询：作为 source 和作为 target）
        # 1. 作为 source → target
        rel_result = await db.execute(
            select(DecisionRelation, Decision)
            .join(Decision, DecisionRelation.target_decision_id == Decision.id)
            .where(DecisionRelation.source_decision_id == decision_id)
            .order_by(DecisionRelation.similarity_score.desc().nullslast())
        )

        # 2. 作为 target ← source
        reverse_rel_result = await db.execute(
            select(DecisionRelation, Decision)
            .join(Decision, DecisionRelation.source_decision_id == Decision.id)
            .where(DecisionRelation.target_decision_id == decision_id)
            .order_by(DecisionRelation.similarity_score.desc().nullslast())
        )

        # 合并去重（避免 A→B 和 B→A 重复显示）
        related_map = {}
        for r, d in rel_result.all():
            related_map[str(d.id)] = {
                "id": str(d.id),
                "title": d.title,
                "similarity_score": r.similarity_score,
                "relation_type": r.relation_type,
                "context": r.context,
            }
        for r, d in reverse_rel_result.all():
            related_map[str(d.id)] = {
                "id": str(d.id),
                "title": d.title,
                "similarity_score": r.similarity_score,
                "relation_type": r.relation_type,
                "context": r.context,
            }

        # 按相似度排序
        related = sorted(
            list(related_map.values()),
            key=lambda x: x["similarity_score"] or 0,
            reverse=True,
        )

        return {
            "id": str(decision.id),
            "meeting_id": str(decision.meeting_id) if decision.meeting_id else None,
            "title": decision.title,
            "context": decision.context,
            "snippet": decision.snippet,
            "chosen_option": decision.chosen_option,
            "reasons": decision.reasons,
            "objections": decision.objections,
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
            "confidence": decision.confidence,
            "evidence": decision.evidence,
            "created_at": decision.created_at.isoformat() if decision.created_at else None,
            "options": [
                {
                    "id": str(o.id),
                    "name": o.name,
                    "pros": o.pros,
                    "cons": o.cons,
                    "proposed_by": o.proposed_by,
                    "is_chosen": o.is_chosen,
                }
                for o in options
            ],
            "related_decisions": related,
        }


# 全局实例
decision_graph_service = DecisionGraphService()
