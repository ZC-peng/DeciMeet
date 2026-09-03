"""API 路由聚合"""

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.meetings import router as meetings_router
from app.api.summaries import router as summaries_router, global_router as summaries_global_router
from app.api.knowledge import router as knowledge_router
from app.api.chat import router as chat_router
from app.api.agent_runs import router as agent_runs_router
from app.api.decisions import router as decisions_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(meetings_router)
api_router.include_router(summaries_global_router)
api_router.include_router(summaries_router)
api_router.include_router(knowledge_router)
api_router.include_router(chat_router)
api_router.include_router(agent_runs_router)
api_router.include_router(decisions_router)
