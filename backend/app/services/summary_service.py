"""纪要生成业务逻辑层"""

import uuid
import logging
import asyncio
import time
from datetime import date
from typing import Optional

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting import Meeting
from app.models.summary import Summary
from app.models.action_item import ActionItem
from app.models.risk import Risk
from app.models.transcript import Transcript
from app.agents.meeting_graph import MeetingAgentState
from app.agents.harness import BudgetGuard, set_harness_context

logger = logging.getLogger(__name__)


# 是否启用 Harness 升级版（v2）。True 用 v2，False 退回 v1。
USE_HARNESS_V2 = True


class SummaryService:
    """纪要生成与管理"""

    def __init__(self) -> None:
        # Prevent duplicate generation for the same meeting in one API process.
        # The DB unique constraint remains the final integrity boundary.
        self._generation_locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def list_summaries(
        self, db: AsyncSession, skip: int = 0, limit: int = 20
    ) -> tuple[list[tuple[Summary, Meeting]], int]:
        """获取所有纪要列表（附带会议信息）"""
        result = await db.execute(
            select(Summary, Meeting)
            .join(Meeting, Summary.meeting_id == Meeting.id)
            .order_by(Summary.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        rows = list(result.all())

        count_result = await db.execute(select(func.count(Summary.id)))
        total = count_result.scalar_one()

        return rows, total

    async def _build_transcript_text(
        self, db: AsyncSession, meeting_id: uuid.UUID
    ) -> str:
        """构建转写文本（供 Agent 使用）"""
        result = await db.execute(
            select(Transcript)
            .where(Transcript.meeting_id == meeting_id)
            .order_by(Transcript.seq_index)
        )
        transcripts = list(result.scalars().all())

        if not transcripts:
            return ""

        lines = []
        for t in transcripts:
            speaker = t.speaker or "未知"
            lines.append(f"[{speaker}]: {t.content}")
        return "\n".join(lines)

    async def generate_summary(
        self, db: AsyncSession, meeting_id: uuid.UUID
    ) -> Optional[Summary]:
        """Serialize duplicate requests for one meeting within this process."""
        lock = self._generation_locks.setdefault(meeting_id, asyncio.Lock())
        async with lock:
            return await self._generate_summary_locked(db, meeting_id)

    async def _generate_summary_locked(
        self, db: AsyncSession, meeting_id: uuid.UUID
    ) -> Optional[Summary]:
        """触发 Multi-Agent 生成纪要（Harness 升级版）"""
        # 1. 获取会议
        meeting_result = await db.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )
        meeting = meeting_result.scalar_one_or_none()
        if not meeting:
            return None

        # 2. 构建转写文本
        transcript_text = await self._build_transcript_text(db, meeting_id)
        if not transcript_text:
            logger.warning(f"会议 {meeting_id} 无转写内容")
            return None

        # 3. Reuse the one summary row for this meeting.  Do not delete the last
        # good snapshot before remote inference succeeds.
        summary_result = await db.execute(
            select(Summary).where(Summary.meeting_id == meeting_id)
        )
        summary = summary_result.scalar_one_or_none()
        # 只保留最后一次真正成功的快照。若上一轮本身就是失败状态，
        # 本轮失败时必须覆盖为最新错误，避免页面一直显示陈旧原因。
        preserve_previous_success = bool(
            summary
            and summary.status == "completed"
            and str(summary.content or "").strip()
        )
        if summary is None:
            summary = Summary(
                meeting_id=meeting_id,
                content="",
                status="generating",
            )
            db.add(summary)
        else:
            summary.status = "generating"
        await db.flush()

        # Persist the visible running state and release the database connection
        # before the long LLM workflow.  The request dependency can safely commit
        # again when the final snapshot has been written.
        await db.commit()

        # 5. 创建 AgentRun（Harness 升级版新增）
        agent_run = None
        if USE_HARNESS_V2:
            try:
                from app.services.agent_run_service import agent_run_service
                agent_run = await agent_run_service.create_run(
                    meeting_id=str(meeting_id),
                    graph_name="meeting_summary_graph_v2",
                    max_tokens=50000,
                )
                await agent_run_service.start_run(agent_run.id)
                logger.info(f"[Harness] AgentRun 创建: {agent_run.id}")
            except Exception as e:
                logger.warning(f"[Harness] AgentRun 创建失败（降级继续）: {e}")
                agent_run = None

        # 6. 构建 BudgetGuard
        budget_guard = BudgetGuard(
            run_id=agent_run.id if agent_run else "",
            max_tokens=50000,
        ) if USE_HARNESS_V2 else None

        # 7. 调用工作流
        if USE_HARNESS_V2:
            return await self._run_v2_workflow(
                db, meeting_id, meeting, transcript_text, summary,
                agent_run, budget_guard, preserve_previous_success,
            )
        else:
            return await self._run_v1_workflow(
                db, meeting_id, meeting, transcript_text, summary,
            )

    async def _run_v2_workflow(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID,
        meeting: Meeting,
        transcript_text: str,
        summary: Summary,
        agent_run,
        budget_guard: BudgetGuard,
        preserve_previous_success: bool,
    ) -> Optional[Summary]:
        """执行 Harness v2 工作流"""
        from app.agents.meeting_graph_v2 import meeting_graph_v2
        from app.services.agent_run_service import agent_run_service

        # 设置 Harness 上下文（contextvar，跨节点传递）
        run_id = agent_run.id if agent_run else ""
        set_harness_context(run_id, budget_guard)

        initial_state = {
            "meeting_id": str(meeting_id),
            "meeting_title": meeting.title,
            "meeting_date": meeting.start_time.isoformat() if meeting.start_time else None,
            "transcript_text": transcript_text,
            "summary": "",
            "key_points": [],
            "action_items": [],
            "risks": [],
            "errors": [],
            # Harness 字段（agent_run_id / budget_guard 通过 contextvar 传递）
            "plan": {},
            "transcript_compressed": False,
            "summary_agent_retry": 0,
            "action_items_agent_retry": 0,
            "risks_agent_retry": 0,
            "valid": False,
            "retry_node": None,
            "retry_reason": None,
            "decisions": [],  # decision_extractor 节点输出
        }

        persistence_started: float | None = None
        try:
            final_state = await meeting_graph_v2.ainvoke(initial_state)
            if agent_run:
                persistence_started = time.perf_counter()
                await agent_run_service.record_step_start(
                    agent_run.id, "persistence"
                )

            errors: list[str] = list(final_state.get("errors") or [])
            summary_content = str(final_state.get("summary") or "").strip()
            if not summary_content:
                errors.append("summary_agent: 返回空纪要")

            if errors:
                unique_errors = list(dict.fromkeys(errors))
                error_msg = unique_errors[0]
                summary.status = "failed"
                if summary_content:
                    summary.content = f"{summary_content}\n\n---\n\n⚠️ 部分内容生成失败：{error_msg}"
                elif not preserve_previous_success:
                    summary.content = f"⚠️ {error_msg}"
                summary.key_points = final_state.get("key_points", [])
                await self._replace_action_items_and_risks(db, meeting_id, final_state)
                await db.flush()
                await db.refresh(summary)
                await db.commit()
                if agent_run:
                    await agent_run_service.record_step_end(
                        agent_run.id,
                        "persistence",
                        "succeeded",
                        int((time.perf_counter() - persistence_started) * 1000),
                    )
                    await agent_run_service.finish_run(
                        agent_run.id,
                        "failed",
                        error="; ".join(unique_errors),
                    )
                logger.warning(f"会议 {meeting_id} 纪要生成部分失败: {error_msg}")
                return summary

            # 成功
            summary.content = summary_content
            summary.key_points = final_state.get("key_points", [])
            summary.status = "completed"
            await self._replace_action_items_and_risks(db, meeting_id, final_state)
            # 先提交用户可见的纪要/行动项/风险，再处理决策档案与 RAG 索引。
            await db.flush()
            await db.commit()

            # 决策是当前项目的核心产物：失败不回滚已经生成的纪要，但必须让
            # AgentRun 显式失败，不能把“纪要成功、决策丢失”伪装成整条链路成功。
            decision_error = None
            decisions = final_state.get("decisions", [])
            if decisions:
                try:
                    from app.services.decision_graph_service import decision_graph_service
                    async with db.begin_nested():
                        await decision_graph_service.save_decisions(
                            db, meeting_id, decisions
                        )
                    await db.commit()
                    logger.info(f"[Harness] 决策落库 {len(decisions)} 条，meeting={meeting_id}")
                except Exception as e:
                    decision_error = f"决策档案落库失败: {e}"
                    logger.warning(decision_error)
            logger.info(f"会议 {meeting_id} 纪要生成完成（Harness v2）")

            # 自动索引到知识库
            try:
                from app.services.knowledge_service import knowledge_service
                async with db.begin_nested():
                    await knowledge_service.index_meeting_summary(
                        db=db,
                        meeting_id=meeting_id,
                        meeting_title=meeting.title,
                        summary_content=summary.content,
                    )
                await db.commit()
            except Exception as idx_err:
                logger.warning(f"知识库索引失败: {idx_err}")

            if agent_run:
                try:
                    await agent_run_service.record_step_end(
                        agent_run.id,
                        "persistence",
                        "failed" if decision_error else "succeeded",
                        int((time.perf_counter() - persistence_started) * 1000),
                        error=decision_error,
                    )
                    await agent_run_service.finish_run(
                        agent_run.id,
                        "failed" if decision_error else "succeeded",
                        error=decision_error,
                    )
                except Exception as run_err:
                    logger.warning("AgentRun 终态更新失败: %s", run_err)

            return summary

        except Exception as e:
            logger.error(f"[Harness v2] 纪要生成失败: {e}")
            summary.status = "failed"
            if not preserve_previous_success:
                summary.content = f"⚠️ 纪要生成异常：{str(e)}"
            await db.flush()
            await db.refresh(summary)
            await db.commit()
            if agent_run:
                try:
                    if persistence_started is not None:
                        await agent_run_service.record_step_end(
                            agent_run.id,
                            "persistence",
                            "failed",
                            int((time.perf_counter() - persistence_started) * 1000),
                            error=str(e),
                        )
                    await agent_run_service.finish_run(
                        agent_run.id, "failed", error=str(e)
                    )
                except Exception:
                    pass
            return summary

    async def _run_v1_workflow(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID,
        meeting: Meeting,
        transcript_text: str,
        summary: Summary,
    ) -> Optional[Summary]:
        """执行 v1 工作流（降级用）"""
        from app.agents.meeting_graph import meeting_graph

        initial_state: MeetingAgentState = {
            "meeting_id": str(meeting_id),
            "meeting_title": meeting.title,
            "transcript_text": transcript_text,
            "summary": "",
            "key_points": [],
            "action_items": [],
            "risks": [],
            "errors": [],
        }

        try:
            final_state = await meeting_graph.ainvoke(initial_state)
            errors: list[str] = list(final_state.get("errors") or [])
            summary_content = str(final_state.get("summary") or "").strip()
            if not summary_content:
                errors.append("summary_agent: 返回空纪要")
            if errors:
                unique_errors = list(dict.fromkeys(errors))
                error_msg = unique_errors[0]
                summary.status = "failed"
                if summary_content:
                    summary.content = f"{summary_content}\n\n---\n\n⚠️ 部分内容生成失败：{error_msg}"
                elif not summary.content:
                    summary.content = f"⚠️ {error_msg}"
                summary.key_points = final_state.get("key_points", [])
                await self._replace_action_items_and_risks(db, meeting_id, final_state)
                await db.flush()
                await db.refresh(summary)
                return summary

            summary.content = summary_content
            summary.key_points = final_state.get("key_points", [])
            summary.status = "completed"
            await self._replace_action_items_and_risks(db, meeting_id, final_state)
            await db.flush()
            await db.refresh(summary)

            try:
                from app.services.knowledge_service import knowledge_service
                async with db.begin_nested():
                    await knowledge_service.index_meeting_summary(
                        db=db,
                        meeting_id=meeting_id,
                        meeting_title=meeting.title,
                        summary_content=summary.content,
                    )
            except Exception as idx_err:
                logger.warning(f"知识库索引失败: {idx_err}")

            return summary

        except Exception as e:
            logger.error(f"纪要生成失败: {e}")
            summary.status = "failed"
            if not summary.content:
                summary.content = f"⚠️ 纪要生成异常：{str(e)}"
            await db.flush()
            await db.refresh(summary)
            return summary

    async def _replace_action_items_and_risks(
        self, db: AsyncSession, meeting_id: uuid.UUID, final_state: dict
    ) -> None:
        """Atomically replace derived rows after inference has returned."""
        await db.execute(
            delete(ActionItem).where(ActionItem.meeting_id == meeting_id)
        )
        await db.execute(delete(Risk).where(Risk.meeting_id == meeting_id))

        for item in final_state.get("action_items", []):
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            due_date = None
            if item.get("due_date"):
                try:
                    due_date = date.fromisoformat(item["due_date"])
                except (ValueError, TypeError):
                    pass

            action_item = ActionItem(
                meeting_id=meeting_id,
                title=title,
                assignee=item.get("assignee"),
                due_date=due_date,
                priority=item.get("priority", "medium"),
                status="pending",
            )
            db.add(action_item)

        for risk in final_state.get("risks", []):
            description = str(risk.get("description") or "").strip()
            if not description:
                continue
            risk_item = Risk(
                meeting_id=meeting_id,
                description=description,
                severity=risk.get("severity", "medium"),
                mitigation=risk.get("mitigation"),
            )
            db.add(risk_item)

    async def get_summary(self, db: AsyncSession, meeting_id: uuid.UUID) -> Optional[Summary]:
        """获取会议纪要"""
        result = await db.execute(
            select(Summary).where(Summary.meeting_id == meeting_id)
        )
        return result.scalar_one_or_none()

    async def get_action_items(self, db: AsyncSession, meeting_id: uuid.UUID) -> list[ActionItem]:
        """获取行动项"""
        result = await db.execute(
            select(ActionItem)
            .where(ActionItem.meeting_id == meeting_id)
            .order_by(ActionItem.created_at)
        )
        return list(result.scalars().all())

    async def get_risks(self, db: AsyncSession, meeting_id: uuid.UUID) -> list[Risk]:
        """获取风险"""
        result = await db.execute(
            select(Risk)
            .where(Risk.meeting_id == meeting_id)
            .order_by(Risk.created_at)
        )
        return list(result.scalars().all())


# 全局实例
summary_service = SummaryService()
