"""知识库业务逻辑层

功能：
1. 文档索引：分块 → 向量化 → 存储
2. 混合检索：向量相似度 + 全文检索，RRF 融合
3. Rerank 重排序（基于关键词匹配评分）
4. 检索结果去重（避免 overlap 分块导致的重复内容）
"""

import os
import uuid
import logging
import difflib
import re
import asyncio
from typing import Optional

from sqlalchemy import select, delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_doc import KnowledgeDocument
from app.services.embedding_service import embedding_service
from app.services.document_chunker import document_chunker
from app.services.document_parser import document_parser

logger = logging.getLogger(__name__)


class KnowledgeService:
    """知识库管理"""

    async def index_text(
        self,
        db: AsyncSession,
        title: str,
        content: str,
        source_type: str = "uploaded_doc",
        source_id: Optional[uuid.UUID] = None,
        metadata: Optional[dict] = None,
    ) -> list[KnowledgeDocument]:
        """索引文本到知识库

        流程：分块 → 向量化 → 存储
        """
        if not content or not content.strip():
            return []

        # 1. 分块
        logical_source_id = source_id or uuid.uuid4()
        base_metadata = {
            **(metadata or {}),
            "title": title,
            "source_type": source_type,
            "document_id": str(logical_source_id),
        }
        chunks = document_chunker.split_with_metadata(content, base_metadata)

        if not chunks:
            return []

        # 2. 批量向量化
        texts = [c["content"] for c in chunks]
        embeddings = await embedding_service.embed_batch(texts)
        if len(embeddings) != len(chunks):
            logger.warning(
                "Embedding 数量与分块不一致：chunks=%s embeddings=%s；缺失项降级为全文检索",
                len(chunks),
                len(embeddings),
            )
            embeddings = list(embeddings[: len(chunks)])
            embeddings.extend([None] * (len(chunks) - len(embeddings)))

        # 3. 存储到数据库
        documents = []
        for chunk, embedding in zip(chunks, embeddings):
            doc = KnowledgeDocument(
                title=title,
                source_type=source_type,
                source_id=logical_source_id,
                content=chunk["content"],
                metadata_=chunk["metadata"],
                embedding=embedding,
            )
            db.add(doc)
            documents.append(doc)

        await db.flush()
        for doc in documents:
            await db.refresh(doc)

        logger.info(f"知识索引完成：{title}，{len(documents)} 个块")
        return documents

    async def index_meeting_summary(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID,
        meeting_title: str,
        summary_content: str,
    ) -> list[KnowledgeDocument]:
        """将会议纪要索引到知识库"""
        # 先删除旧的索引
        await db.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.source_id == meeting_id,
                KnowledgeDocument.source_type == "meeting_summary",
            )
        )
        await db.flush()

        return await self.index_text(
            db=db,
            title=f"会议纪要：{meeting_title}",
            content=summary_content,
            source_type="meeting_summary",
            source_id=meeting_id,
            metadata={"meeting_id": str(meeting_id)},
        )

    async def index_document_file(
        self,
        db: AsyncSession,
        file_path: str,
        filename: str,
    ) -> Optional[list[KnowledgeDocument]]:
        """解析文档文件并索引

        同名文档会被覆盖（先删除旧 chunk，再插入新 chunk），避免重复。
        """
        # 1. 解析文档
        content = await asyncio.to_thread(document_parser.parse, file_path, filename)
        if not content:
            return None

        # 2. 索引
        title = os.path.splitext(filename)[0]

        # 先删除同名文档的所有 chunk（避免重复上传导致数据堆积）
        await db.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.title == title,
                KnowledgeDocument.source_type == "uploaded_doc",
            )
        )
        await db.flush()

        return await self.index_text(
            db=db,
            title=title,
            content=content,
            source_type="uploaded_doc",
            metadata={"filename": filename},
        )

    async def search(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
    ) -> list[dict]:
        """混合检索：向量 + 全文，RRF 融合

        Args:
            query: 查询文本
            top_k: 返回结果数量

        Returns:
            检索结果列表，每项包含 content, title, score, metadata
        """
        query = query.strip()
        if not query:
            return []
        top_k = max(1, min(int(top_k), 50))
        candidate_limit = min(max(top_k * 4, 20), 200)

        # 1. 向量化查询
        query_embedding = await embedding_service.embed_text(query)

        # 2. 向量检索（余弦相似度）
        vector_results = (
            await self._vector_search(db, query_embedding, candidate_limit)
            if query_embedding
            else []
        )

        # 3. 全文检索
        fulltext_results = await self._fulltext_search(
            db, query, candidate_limit
        )

        # 4. RRF 融合
        fused = self._rrf_fusion(vector_results, fulltext_results)

        if not fused:
            logger.warning("知识检索无可用候选：query=%r", query[:100])
            return []

        # 5. Rerank（基于关键词匹配）
        reranked = self._rerank(query, fused)

        # 6. 先去重再截断，否则 overlap chunk 会占掉 top-k 配额。
        deduped = self._deduplicate(reranked, threshold=0.7)
        final_results = deduped[:top_k]
        for item in final_results:
            item["score"] = float(
                item.get("rerank_score", item.get("score", 0.0))
            )
        return final_results

    async def _vector_search(
        self, db: AsyncSession, query_embedding: list[float], limit: int
    ) -> list[dict]:
        """向量相似度检索

        使用 pgvector 的 cosine_distance，并转换为相似度分数：
        similarity = 1 - cosine_distance（范围 0~1，1 表示完全相似）
        """
        try:
            # 使用 with_entities 同时取距离，避免加载整个 ORM 对象
            stmt = (
                select(
                    KnowledgeDocument,
                    KnowledgeDocument.embedding.cosine_distance(query_embedding).label("distance"),
                )
                .where(KnowledgeDocument.embedding.is_not(None))
                .order_by("distance")
                .limit(limit)
            )
            async with db.begin_nested():
                result = await db.execute(stmt)
                rows = result.all()

            documents = []
            for doc, distance in rows:
                if distance is None:
                    continue
                documents.append({
                    "id": str(doc.id),
                    "content": doc.content,
                    "title": doc.title,
                    "source_type": doc.source_type,
                    "source_id": str(doc.source_id) if doc.source_id else None,
                    "metadata": doc.metadata_ or {},
                    "vector_score": max(0.0, 1.0 - float(distance)),
                })
            return documents
        except Exception as e:
            logger.error(f"向量检索失败: {e}")
            return []

    async def _fulltext_search(
        self, db: AsyncSession, query: str, limit: int
    ) -> list[dict]:
        """全文检索（PostgreSQL tsvector + ts_rank）

        使用 plainto_tsquery + simple 配置计算词法相关性。simple 配置
        并不提供专门的中文分词，因此中文语义召回主要依靠向量路由，
        ILIKE 只作为数据库兼容性兜底。
        """
        try:
            # plainto_tsquery 自动处理查询分词（&连接）
            # ts_rank 按词频和位置计算相关性
            ts_query = func.plainto_tsquery("simple", query)
            rank = func.ts_rank(
                func.to_tsvector("simple", KnowledgeDocument.content),
                ts_query,
            )

            stmt = (
                select(KnowledgeDocument, rank.label("rank"))
                .where(rank > 0)
                .order_by(text("rank DESC"))
                .limit(limit)
            )
            async with db.begin_nested():
                result = await db.execute(stmt)
                rows = result.all()
            if not rows:
                return await self._fulltext_search_fallback(db, query, limit)
            return [
                {
                    "id": str(doc.id),
                    "content": doc.content,
                    "title": doc.title,
                    "source_type": doc.source_type,
                    "source_id": str(doc.source_id) if doc.source_id else None,
                    "metadata": doc.metadata_ or {},
                    "fulltext_score": float(rank_val),
                }
                for doc, rank_val in rows
            ]
        except Exception as e:
            logger.error(f"全文检索失败: {e}")
            # 降级到 ILIKE
            return await self._fulltext_search_fallback(db, query, limit)

    async def _fulltext_search_fallback(
        self, db: AsyncSession, query: str, limit: int
    ) -> list[dict]:
        """全文检索降级方案：ILIKE（兼容性兜底）"""
        try:
            safe_query = query.replace('%', '\\%').replace('_', '\\_')
            pattern = f"%{safe_query}%"
            stmt = (
                select(KnowledgeDocument)
                .where(KnowledgeDocument.content.ilike(pattern, escape='\\'))
                .order_by(KnowledgeDocument.created_at.desc())
                .limit(limit)
            )
            async with db.begin_nested():
                result = await db.execute(stmt)
                docs = list(result.scalars().all())

            return [
                {
                    "id": str(doc.id),
                    "content": doc.content,
                    "title": doc.title,
                    "source_type": doc.source_type,
                    "source_id": str(doc.source_id) if doc.source_id else None,
                    "metadata": doc.metadata_ or {},
                    "fulltext_score": 1.0,
                }
                for doc in docs
            ]
        except Exception as e:
            logger.error(f"全文检索降级也失败: {e}")
            return []

    def _rrf_fusion(
        self,
        vector_results: list[dict],
        fulltext_results: list[dict],
        k: int = 60,
    ) -> list[dict]:
        """RRF（Reciprocal Rank Fusion）融合

        公式：score = 1 / (k + rank)
        """
        scores: dict[str, float] = {}
        docs_map: dict[str, dict] = {}

        # 向量检索结果
        for rank, item in enumerate(vector_results):
            doc_id = item["id"]
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
            docs_map[doc_id] = item

        # 全文检索结果
        for rank, item in enumerate(fulltext_results):
            doc_id = item["id"]
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
            if doc_id not in docs_map:
                docs_map[doc_id] = item
            else:
                # Preserve channel-specific scores and any metadata populated by
                # either retriever instead of silently discarding the second hit.
                docs_map[doc_id] = {**docs_map[doc_id], **item}

        # 按融合分数排序
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        result = []
        for doc_id in sorted_ids:
            item = docs_map[doc_id].copy()
            item["score"] = scores[doc_id]
            result.append(item)

        return result

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        """Extract useful lexical terms for both whitespace and Chinese text."""
        lowered = query.lower()
        terms = set(re.findall(r"[a-z0-9_]+", lowered))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
            if len(sequence) == 1:
                terms.add(sequence)
            else:
                terms.update(
                    sequence[index : index + 2]
                    for index in range(len(sequence) - 1)
                )
        return terms

    def _rerank(self, query: str, results: list[dict]) -> list[dict]:
        """简单的 Rerank：基于关键词匹配评分

        实际生产环境应使用 bge-reranker-v2-m3 等模型
        """
        query_terms = self._query_terms(query)
        if not query_terms:
            return results

        scored_results = []
        for original in results:
            item = original.copy()
            content = item.get("content", "").lower()
            # 计算关键词命中率
            hits = sum(1 for term in query_terms if term in content)
            match_ratio = hits / len(query_terms) if query_terms else 0

            # 融合原始分数与匹配率
            base_score = item.get("score", 0)
            item["rerank_score"] = base_score * 0.7 + match_ratio * 0.3
            scored_results.append(item)

        # 按 rerank_score 排序
        sorted_results = sorted(
            scored_results, key=lambda x: x.get("rerank_score", 0), reverse=True
        )

        # 归一化到 0~1：最高分映射为 1.0
        # 原始 rerank_score 受 RRF base_score 影响上限约 0.33，
        # 用户直接看百分比会误以为相关度很低，需归一化
        if sorted_results:
            max_score = sorted_results[0].get("rerank_score", 0)
            if max_score > 0:
                for item in sorted_results:
                    item["rerank_score"] = round(
                        item.get("rerank_score", 0) / max_score, 4
                    )

        return sorted_results

    def _deduplicate(
        self, results: list[dict], threshold: float = 0.7
    ) -> list[dict]:
        """去重：移除内容高度相似的块

        文档分块时 overlap=200 会导致相邻块内容 60-80% 重复，
        这些块在 top_k 结果中会挤占名额，降低信息覆盖度。

        使用 SequenceMatcher 计算相似度，超过阈值则跳过。
        """
        if len(results) <= 1:
            return results

        deduped: list[dict] = []
        for item in results:
            content = item.get("content", "")
            is_dup = False
            for kept in deduped:
                kept_content = kept.get("content", "")
                # SequenceMatcher 计算最长公共子序列比例
                ratio = difflib.SequenceMatcher(
                    None, content, kept_content
                ).quick_ratio()
                if ratio >= threshold:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(item)

        if len(deduped) < len(results):
            logger.debug(
                f"去重: {len(results)} → {len(deduped)} 个块"
            )
        return deduped

    async def list_documents(
        self, db: AsyncSession, skip: int = 0, limit: int = 20
    ) -> tuple[list[KnowledgeDocument], int]:
        """获取知识文档列表（按 title 去重，每个文档只显示一条记录）

        一个文档可能被分成多个 chunk，这里只返回每个 title 的第一条记录。
        """
        # Window function first selects the newest chunk per logical source,
        # then applies global recency ordering.  This avoids pagination over a
        # title-sorted DISTINCT ON result.
        row_number = func.row_number().over(
            partition_by=(
                KnowledgeDocument.source_type,
                KnowledgeDocument.source_id,
                KnowledgeDocument.title,
            ),
            order_by=KnowledgeDocument.created_at.desc(),
        ).label("row_number")
        ranked = select(KnowledgeDocument.id, row_number).subquery()
        result = await db.execute(
            select(KnowledgeDocument)
            .join(ranked, ranked.c.id == KnowledgeDocument.id)
            .where(ranked.c.row_number == 1)
            .order_by(KnowledgeDocument.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        docs = list(result.scalars().all())

        # Count the same logical groups used by the list query.
        groups = (
            select(
                KnowledgeDocument.source_type,
                KnowledgeDocument.source_id,
                KnowledgeDocument.title,
            )
            .distinct()
            .subquery()
        )
        count_result = await db.execute(
            select(func.count()).select_from(groups)
        )
        total = count_result.scalar_one()

        return docs, total

    async def delete_document(self, db: AsyncSession, doc_id: uuid.UUID) -> bool:
        """删除知识文档（按 title 删除所有相关 chunk）"""
        result = await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )
        doc = result.scalar_one_or_none()
        if not doc:
            return False

        # New rows share one source_id across their chunks.  Fall back to the
        # legacy source_type/title grouping only for historical rows without it.
        if doc.source_id:
            predicate = (
                (KnowledgeDocument.source_type == doc.source_type)
                & (KnowledgeDocument.source_id == doc.source_id)
            )
        else:
            predicate = (
                (KnowledgeDocument.source_type == doc.source_type)
                & (KnowledgeDocument.title == doc.title)
            )
        await db.execute(delete(KnowledgeDocument).where(predicate))
        await db.flush()
        return True


# 全局实例
knowledge_service = KnowledgeService()
