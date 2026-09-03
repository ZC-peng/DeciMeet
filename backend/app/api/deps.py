"""API 依赖注入"""

import uuid
from typing import AsyncGenerator

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.meeting import Meeting
from app.services.meeting_service import meeting_service


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库 Session"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_meeting_or_404(
    meeting_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Meeting:
    """获取会议，不存在则抛 404"""
    meeting = await meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting
