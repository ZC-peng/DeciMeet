"""Embedding 向量化服务"""

import logging
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """文本向量化服务，基于通义千问 text-embedding-v3"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        ) if settings.OPENAI_API_KEY else None
        self.model = settings.EMBEDDING_MODEL
        # text-embedding-v3 维度
        self.dimensions = settings.EMBEDDING_DIMENSIONS

    async def embed_text(self, text: str) -> Optional[list[float]]:
        """单文本向量化"""
        if not self.client:
            logger.warning("OPENAI_API_KEY 未配置，跳过向量化")
            return None

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=text,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"向量化失败: {e}")
            return None

    async def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """批量向量化"""
        if not self.client:
            return [None] * len(texts)

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=texts,
            )
            # 按 index 排序确保顺序
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [d.embedding for d in sorted_data]
        except Exception as e:
            logger.error(f"批量向量化失败: {e}")
            return [None] * len(texts)


# 全局实例
embedding_service = EmbeddingService()
