"""决策库 API：列表 / 搜索 / 详情

当前仅提供只读 API（list / search / detail），
不做 POST/PUT/DELETE（决策由 Agent 自动抽取，不允许人工编辑）
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.services.decision_graph_service import decision_graph_service

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("")
async def list_decisions(
    meeting_id: uuid.UUID | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """决策列表（分页 + 按 meeting 筛选）

    Args:
        meeting_id: 按会议筛选（可选）
        skip: 分页偏移
        limit: 每页数量（1~100）
    """
    decisions, total = await decision_graph_service.list_decisions(
        db, meeting_id=meeting_id, skip=skip, limit=limit
    )
    return {
        "items": [
            {
                "id": str(d.id),
                "title": d.title,
                "chosen_option": d.chosen_option,
                "meeting_id": str(d.meeting_id) if d.meeting_id else None,
                "decided_by": d.decided_by,
                "confidence": d.confidence,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/search")
async def search_decisions(
    q: str = Query(..., min_length=1, description="关键词或语义查询"),
    top_k: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    """决策语义搜索（基于 pgvector cosine 相似度）

    适用于「为什么选 X」「X 选型决策」类查询
    """
    results = await decision_graph_service.search(db, q, top_k=top_k)
    return {
        "items": results,
        "query": q,
        "total": len(results),
    }


@router.get("/{decision_id}")
async def get_decision(
    decision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """决策详情（含 options + 关联决策）

    关联决策由写入时即时向量关联生成，当前 relation_type 统一为
    'relates'，后续可进一步细化关系类型。
    """
    detail = await decision_graph_service.get_decision(db, decision_id)
    if not detail:
        raise HTTPException(status_code=404, detail="决策不存在")
    return detail
