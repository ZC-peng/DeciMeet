"""对话服务：文档混合检索 + 决策语义检索 + 流式生成。"""

import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chat import ChatSession, ChatMessage
from app.services.knowledge_service import knowledge_service
from app.services.decision_graph_service import decision_graph_service

logger = logging.getLogger(__name__)

# 系统提示词
SYSTEM_PROMPT = """你是 AI 会议助手，专注于企业会议场景的智能问答。

你的能力：
1. 基于知识库与决策库的检索结果回答会议相关问题
2. 总结会议要点、解释行动项、识别风险
3. 回答历史评审中做过的决策、对比相似决策
4. 当用户问题与会议/知识库无关时，礼貌引导回会议主题

回答规范：
- 优先使用【知识库】和【决策库】中的内容回答
- 引用知识时标注来源（如"根据会议纪要..."、"根据历史决策..."）
- 回答决策相关问题时，说明已选方案、候选方案与选择理由
- 使用 Markdown 格式输出，结构清晰
- 如检索结果为空或不相关，基于通用知识回答但需说明

当前检索结果：
{context}
"""


class ChatService:
    """对话服务"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        ) if settings.OPENAI_API_KEY else None
        self.model = settings.LLM_MODEL

    async def create_session(
        self,
        db: AsyncSession,
        meeting_id: str | None = None,
        title: str | None = None,
    ) -> ChatSession:
        """创建对话会话"""
        session = ChatSession(
            meeting_id=meeting_id,
            title=title or "新对话",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def list_sessions(
        self,
        db: AsyncSession,
        meeting_id: str | None = None,
    ) -> list[ChatSession]:
        """获取会话列表"""
        stmt = select(ChatSession).order_by(ChatSession.created_at.desc())
        if meeting_id:
            stmt = stmt.where(ChatSession.meeting_id == meeting_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_session_messages(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> list[ChatMessage]:
        """获取会话消息历史"""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def save_message(
        self,
        db: AsyncSession,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> ChatMessage:
        """保存消息"""
        msg = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg

    async def chat_stream(
        self,
        db: AsyncSession,
        session_id: str,
        query: str,
        images: list[str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """流式对话：RAG 检索 + LLM 流式生成

        Args:
            images: base64 编码的图片列表（data URL），用于多模态对话
        通过 SSE 逐步返回内容。
        """
        if not self.client:
            yield {"type": "error", "message": "LLM 未配置"}
            return

        # 1. 保存用户消息（含图片元信息）
        await self.save_message(
            db,
            session_id,
            "user",
            query,
            metadata={"images": images} if images else None,
        )

        # 2. 获取历史消息（最近 10 条）
        history = await self.get_session_messages(db, session_id)
        history_msgs = [
            {"role": m.role, "content": m.content}
            for m in history[-10:]  # 最近 10 条
        ]

        # 3. 查询改写：多轮对话中消解指代（"它"、"这个"等）
        # 仅当存在历史且查询简短时触发，节省 token
        rewritten_query = await self._rewrite_query(query, history_msgs[:-1]) if len(history_msgs) > 1 else query
        logger.info(f"查询改写: {query!r} → {rewritten_query!r}")

        # 4. 双路 RAG 检索：文档混合检索 + 决策语义检索。
        try:
            doc_results = await knowledge_service.search(db, rewritten_query, top_k=3)
        except Exception as e:
            logger.warning(f"知识库检索失败: {e}")
            doc_results = []

        try:
            decision_results = await decision_graph_service.search(db, rewritten_query, top_k=3)
        except Exception as e:
            logger.warning(f"决策库检索失败: {e}")
            decision_results = []

        # 两路分数语义不同：文档路是 rerank score，决策路是 cosine
        # similarity。先在各自路由内归一化，再按查询意图加权合并。
        fused = self._merge_retrieval_routes(
            rewritten_query, doc_results, decision_results, top_k=5
        )

        # 构建上下文（区分来源类型）
        if fused:
            context_parts = []
            for i, r in enumerate(fused, 1):
                source_type = r.get("source_type", "")
                if source_type == "decision":
                    title = r.get("title", "未知决策")
                    chosen = r.get("chosen_option")
                    context_text = r.get("context", "")
                    parts = [f"[{i}] 来源：决策库 - {title}"]
                    if chosen:
                        parts.append(f"已选方案：{chosen}")
                    if context_text:
                        parts.append(f"背景：{context_text[:400]}")
                    context_parts.append("\n".join(parts))
                else:
                    source = "会议纪要" if source_type == "meeting_summary" else "文档"
                    context_parts.append(
                        f"[{i}] 来源：{source} - {r.get('title', '未知')}\n"
                        f"内容：{r.get('content', '')[:500]}"
                    )
            context = "\n\n".join(context_parts)
        else:
            context = "（未检索到相关知识或决策）"

        # 5. 构建消息列表
        # 有图片时使用多模态模型 qwen-vl-plus
        has_images = bool(images)
        model = settings.VISION_MODEL if has_images else self.model

        if has_images:
            # 多模态：当前用户消息包含文本 + 图片
            user_content: list[dict] = [{"type": "text", "text": query}]
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
                *[
                    {"role": m["role"], "content": m["content"]}
                    for m in history_msgs[:-1]  # 排除当前用户消息（已含图片）
                ],
                {"role": "user", "content": user_content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
                *history_msgs,
            ]

        # 6. 流式生成
        full_response = ""
        llm_failed = False
        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=0.7,
            )

            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    full_response += delta
                    yield {"type": "token", "content": delta}

        except Exception as e:
            logger.error(f"LLM 流式生成失败: {e}")
            # 分类错误并返回友好提示，避免暴露 request_id 等技术细节
            from app.agents.meeting_graph import _classify_llm_error
            friendly_msg = _classify_llm_error(e)
            llm_failed = True
            yield {"type": "error", "message": friendly_msg}

        # 7. 保存助手回复（仅当未失败且内容非空时）
        # 失败时不保存，避免污染历史对话
        sources = [
            {
                "title": r.get("title") or "未知来源",
                "source_type": r.get("source_type") or "document",
                "score": r.get("retrieval_score", r.get("score")),
            }
            for r in fused
        ]
        if full_response and not llm_failed:
            await self.save_message(
                db,
                session_id,
                "assistant",
                full_response,
                metadata={
                    "sources": sources
                } if fused else None,
            )
            yield {"type": "done", "sources": sources}
        elif not llm_failed:
            yield {"type": "error", "message": "模型未返回有效内容，请重试"}

    def _merge_retrieval_routes(
        self,
        query: str,
        doc_results: list[dict],
        decision_results: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """按路由归一化并合并异构结果。

        文档路已经在同一文档集合上完成向量/全文 RRF；决策路来自另一张表，
        直接再次称为 RRF 会掩盖两路分数不可比的问题。因此这里在每条路由内
        归一化相关度，再组合路由内排名，并对决策意图做轻量加权。
        """
        decision_intent = any(
            term in query.lower()
            for term in (
                "决策", "决定", "拍板", "方案", "取舍", "为什么选",
                "decision", "option", "choose", "chose",
            )
        )
        route_weights = {
            "document": 1.0,
            "decision": 1.15 if decision_intent else 0.95,
        }

        merged: list[dict] = []
        for route, results in (
            ("document", doc_results),
            ("decision", decision_results),
        ):
            raw_scores = [
                max(
                    0.0,
                    float(
                        item.get("rerank_score", item.get("score", 0.0)) or 0.0
                    ),
                )
                for item in results
            ]
            max_score = max(raw_scores, default=0.0) or 1.0
            for rank, (item, raw_score) in enumerate(zip(results, raw_scores)):
                normalized_relevance = raw_score / max_score
                rank_prior = 1.0 / (rank + 1)
                retrieval_score = route_weights[route] * (
                    0.75 * normalized_relevance + 0.25 * rank_prior
                )
                enriched = item.copy()
                enriched["retrieval_route"] = route
                enriched["retrieval_score"] = round(retrieval_score, 4)
                merged.append(enriched)

        return sorted(
            merged,
            key=lambda item: item["retrieval_score"],
            reverse=True,
        )[:top_k]

    async def _rewrite_query(self, query: str, history: list[dict]) -> str:
        """查询改写：消解多轮对话中的指代词

        将依赖上下文的查询（如"它的截止日期是什么时候？"）改写为独立查询
        （如"行动项『完成产品需求文档』的截止日期是什么时候？"）。

        仅在查询简短（可能含指代）时触发，避免不必要的 LLM 调用。
        """
        # 简单启发式：查询过短或包含指代词时才改写
        if len(query) > 50 or not history:
            return query

        # 检测指代词（中英文）
        ref_keywords = ["它", "他", "她", "这个", "那个", "这些", "那些", "其",
                        "it", "this", "that", "these", "those", "they", "them"]
        query_lower = query.lower()
        if not any(kw in query_lower for kw in ref_keywords):
            return query

        rewrite_prompt = [
            {"role": "system", "content": (
                "你是查询改写助手。根据对话历史，将用户最新查询中的指代词替换为具体内容，"
                "使其成为不依赖上下文也能理解的独立查询。\n"
                "只输出改写后的查询，不要解释、不要引号。如果无需改写，原样输出。"
            )},
            *[
                {"role": m["role"], "content": m["content"][:200] if isinstance(m["content"], str) else str(m["content"])[:200]}
                for m in history[-4:]  # 最近 4 条上下文
            ],
            {"role": "user", "content": f"请改写此查询: {query}"},
        ]

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=rewrite_prompt,
                temperature=0.0,
                max_tokens=100,
            )
            rewritten = resp.choices[0].message.content.strip()
            # 防御：避免模型返回空或过长解释
            if rewritten and len(rewritten) < 200:
                return rewritten
            return query
        except Exception as e:
            logger.warning(f"查询改写失败，使用原始查询: {e}")
            return query

    async def delete_session(self, db: AsyncSession, session_id: str) -> bool:
        """删除会话"""
        stmt = select(ChatSession).where(ChatSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if session:
            await db.delete(session)
            await db.commit()
            return True
        return False


chat_service = ChatService()
