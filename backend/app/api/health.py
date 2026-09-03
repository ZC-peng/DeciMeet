"""健康检查接口"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查：验证应用与数据库连接"""
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar_one()
        return {
            "status": "ok",
            "service": f"{settings.APP_NAME} API",
            "database": "healthy",
        }
    except Exception as e:
        logger.warning("数据库健康检查失败: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": f"{settings.APP_NAME} API",
                "database": "unhealthy",
            },
        )
