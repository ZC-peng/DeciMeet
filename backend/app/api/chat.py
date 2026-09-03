"""对话 API：会话管理 + SSE 流式对话"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.chat import ChatSession
from app.services.chat_service import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


# ── 请求/响应模型 ──

class CreateSessionRequest(BaseModel):
    meeting_id: str | None = None
    title: str | None = None


class ChatRequest(BaseModel):
    query: str
    images: list[str] | None = None  # base64 data URL 列表，用于多模态对话


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    metadata: dict | None = None
    created_at: str


class SessionResponse(BaseModel):
    id: str
    meeting_id: str | None = None
    title: str | None = None
    created_at: str


# ── 会话管理 ──

@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    req: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    """创建对话会话"""
    session = await chat_service.create_session(
        db,
        meeting_id=req.meeting_id,
        title=req.title,
    )
    return SessionResponse(
        id=str(session.id),
        meeting_id=str(session.meeting_id) if session.meeting_id else None,
        title=session.title,
        created_at=session.created_at.isoformat(),
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    meeting_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """获取会话列表"""
    sessions = await chat_service.list_sessions(db, meeting_id)
    return [
        SessionResponse(
            id=str(s.id),
            meeting_id=str(s.meeting_id) if s.meeting_id else None,
            title=s.title,
            created_at=s.created_at.isoformat(),
        )
        for s in sessions
    ]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取会话消息历史"""
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID")

    msgs = await chat_service.get_session_messages(db, session_id)
    return [
        MessageResponse(
            id=str(m.id),
            session_id=str(m.session_id),
            role=m.role,
            content=m.content,
            metadata=m.metadata_,
            created_at=m.created_at.isoformat(),
        )
        for m in msgs
    ]


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """删除会话"""
    ok = await chat_service.delete_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "已删除"}


# ── SSE 流式对话 ──

@router.post("/sessions/{session_id}/stream")
async def chat_stream(
    session_id: str,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """SSE 流式对话

    返回 Server-Sent Events：
    - data: {"type": "token", "content": "..."}  增量内容
    - data: {"type": "done", "sources": [...]}  完成
    - data: {"type": "error", "message": "..."} 错误
    """
    try:
        parsed_session_id = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID")

    if not req.query.strip():
        raise HTTPException(status_code=400, detail="查询不能为空")
    if await db.get(ChatSession, parsed_session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    async def event_generator():
        try:
            async for event in chat_service.chat_stream(
                db, session_id, req.query, req.images
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            yield f"data: {json.dumps({'type': 'error', 'message': '对话处理失败，请稍后重试'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
