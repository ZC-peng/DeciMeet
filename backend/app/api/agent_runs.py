"""Agent Run API 路由

提供 Agent 运行记录与步骤时间线查询接口。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.services.agent_run_service import agent_run_service

router = APIRouter(prefix="/agent-runs", tags=["Agent 运行管理"])

@router.get("")
async def list_agent_runs(
    meeting_id: str | None = Query(None, description="按会议 ID 过滤"),
    status: str | None = Query(None, description="按状态过滤: pending/running/succeeded/failed/cancelled"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """获取 Agent Run 列表"""
    skip = (page - 1) * page_size
    runs, total = await agent_run_service.list_runs(
        db, meeting_id=meeting_id, status=status, skip=skip, limit=page_size
    )
    return {
        "items": [r.to_dict() for r in runs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats/overview")
async def get_stats_overview(db: AsyncSession = Depends(get_db)):
    """Agent 运行统计概览（Dashboard 用）"""
    from sqlalchemy import select, func
    from app.models.agent_run import AgentRun

    # 按状态统计
    status_stmt = (
        select(AgentRun.status, func.count(AgentRun.id))
        .group_by(AgentRun.status)
    )
    status_result = await db.execute(status_stmt)
    status_counts = {row[0]: row[1] for row in status_result.all()}

    # 总 Token
    totals_stmt = select(
        func.sum(AgentRun.total_tokens).label("total_tokens"),
        func.count(AgentRun.id).label("total_runs"),
    )
    totals_result = await db.execute(totals_stmt)
    totals = totals_result.first()

    return {
        "status_counts": status_counts,
        "total_runs": totals.total_runs or 0,
        "total_tokens": totals.total_tokens or 0,
        "success_rate": (
            status_counts.get("succeeded", 0) / totals.total_runs
            if totals.total_runs else 0
        ),
    }


@router.get("/{run_id}")
async def get_agent_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个 Agent Run 详情"""
    run = await agent_run_service.get_run(run_id, db=db)
    if not run:
        raise HTTPException(status_code=404, detail="Agent Run 不存在")
    return run.to_dict()
