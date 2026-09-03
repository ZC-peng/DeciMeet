"""AgentRun 生命周期管理服务

负责 AgentRun 的创建、状态流转、节点 step 记录与 Token 预算更新。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import async_session_factory
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


class AgentRunService:
    """AgentRun 生命周期管理"""

    @staticmethod
    async def _get_for_update(db: AsyncSession, run_id: str) -> AgentRun | None:
        """锁定单条 Run，避免并行 Agent 的 JSONB 读改写互相覆盖。"""
        result = await db.execute(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _summarize_node_usage(node_usage: dict) -> tuple[int, int]:
        """Return cumulative input/output tokens from node-level usage."""
        input_tokens = 0
        output_tokens = 0
        for usage in (node_usage or {}).values():
            if not isinstance(usage, dict):
                continue
            input_tokens += max(0, int(usage.get("input_tokens", 0) or 0))
            output_tokens += max(0, int(usage.get("output_tokens", 0) or 0))
        return input_tokens, output_tokens

    # ── 创建与状态流转 ──

    async def create_run(
        self,
        meeting_id: str,
        graph_name: str = "meeting_summary_graph_v2",
        max_tokens: int = 50000,
        max_cost_usd: float = 0.5,
    ) -> AgentRun:
        """创建新的 AgentRun 记录"""
        async with async_session_factory() as db:
            run = AgentRun(
                meeting_id=uuid.UUID(str(meeting_id)),
                graph_name=graph_name,
                status="pending",
                max_tokens=max_tokens,
                max_cost_usd=max_cost_usd,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

    async def start_run(self, run_id: str, thread_id: Optional[str] = None) -> None:
        """标记 Run 为 running"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                    error=None,
                    thread_id=thread_id,
                )
            )
            await db.commit()

    async def finish_run(
        self,
        run_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """标记 Run 完成（succeeded / failed / cancelled）"""
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"非法 AgentRun 终态: {status}")
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(timezone.utc),
                    current_node=None,
                    error=error,
                )
            )
            await db.commit()

    async def set_current_node(self, run_id: str, node: str) -> None:
        """更新当前节点"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(current_node=node)
            )
            await db.commit()

    async def save_plan(self, run_id: str, plan: dict) -> None:
        """保存 Planner 输出的执行计划"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(plan=plan)
            )
            await db.commit()

    # ── 节点 step 记录 ──

    async def record_step_start(self, run_id: str, node: str) -> None:
        """记录节点开始"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await self._get_for_update(db, run_id)
                if not run:
                    logger.warning(f"record_step_start: run_id={run_id} 不存在")
                    return
                steps = list(run.steps or [])
                steps.append({
                    "node": node,
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                })
                # 强制标记字段变更（JSONB 默认不可变检测）
                run.steps = steps
                run.current_node = node
                flag_modified(run, "steps")
                await db.commit()
        except Exception as e:
            logger.warning(f"record_step_start 失败: {e}")

    async def record_step_end(
        self,
        run_id: str,
        node: str,
        status: str,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """记录节点结束"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await self._get_for_update(db, run_id)
                if not run:
                    return
                steps = list(run.steps or [])
                # 找到最后一个同名节点（支持重试）
                for step in reversed(steps):
                    if step.get("node") == node and step.get("status") == "running":
                        step["status"] = status
                        step["finished_at"] = datetime.now(timezone.utc).isoformat()
                        step["duration_ms"] = duration_ms
                        if error:
                            step["error"] = error
                        break
                run.steps = steps
                flag_modified(run, "steps")
                await db.commit()
        except Exception as e:
            logger.warning(f"record_step_end 失败: {e}")

    # ── 预算更新 ──

    async def update_budget(
        self,
        run_id: str,
        used_tokens: int,
        used_cost: float,
        node_usage: dict,
    ) -> None:
        """更新累计预算（``used_cost`` 的单位固定为 USD）。"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await self._get_for_update(db, run_id)
                if not run:
                    return
                normalized_usage = dict(node_usage or {})
                input_tokens, output_tokens = self._summarize_node_usage(
                    normalized_usage
                )
                derived_total = input_tokens + output_tokens

                run.input_tokens = input_tokens
                run.output_tokens = output_tokens
                # Backward-compatible fallback for historical node_usage that
                # only exposed an aggregate ``tokens`` value.
                run.total_tokens = derived_total or max(0, int(used_tokens or 0))
                run.total_cost_usd = max(0.0, float(used_cost or 0.0))
                run.node_usage = normalized_usage
                flag_modified(run, "node_usage")
                await db.commit()
        except Exception as e:
            logger.debug(f"update_budget 失败: {e}")

    # ── 查询 ──

    async def get_run(self, run_id: str, db: AsyncSession | None = None) -> Optional[AgentRun]:
        """获取单个 Run"""
        if db:
            return await db.get(AgentRun, run_id)
        async with async_session_factory() as session:
            return await session.get(AgentRun, run_id)

    async def list_runs(
        self,
        db: AsyncSession,
        meeting_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[AgentRun], int]:
        """获取 Run 列表"""
        stmt = select(AgentRun)
        if meeting_id:
            stmt = stmt.where(AgentRun.meeting_id == meeting_id)
        if status:
            stmt = stmt.where(AgentRun.status == status)
        stmt = stmt.order_by(AgentRun.created_at.desc()).offset(skip).limit(limit)

        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        count_stmt = select(func.count(AgentRun.id))
        if meeting_id:
            count_stmt = count_stmt.where(AgentRun.meeting_id == meeting_id)
        if status:
            count_stmt = count_stmt.where(AgentRun.status == status)
        total = (await db.execute(count_stmt)).scalar_one()

        return rows, total


# 全局实例
agent_run_service = AgentRunService()
