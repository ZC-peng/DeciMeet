"""文档分块器

递归字符分块策略：
- chunk_size: 1000 字符
- overlap: 200 字符
- 优先按段落分割，其次按句子
"""

import re
import logging
from typing import Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


class DocumentChunker:
    """文档分块器"""

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: Optional[list[str]] = None,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        # 默认分隔符：段落 > 换行 > 句号 > 空格
        if separators is None:
            separators = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]

        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators,
            keep_separator=True,
        )

    def split(self, text: str) -> list[str]:
        """将文本分割为块"""
        if not text or not text.strip():
            return []

        chunks = self.splitter.split_text(text)
        # 过滤空块
        chunks = [c.strip() for c in chunks if c.strip()]

        logger.info(f"文档分块完成：{len(chunks)} 个块")
        return chunks

    def split_with_metadata(
        self, text: str, base_metadata: Optional[dict] = None
    ) -> list[dict]:
        """分块并附带元数据"""
        chunks = self.split(text)
        base_metadata = base_metadata or {}

        result = []
        for i, chunk in enumerate(chunks):
            metadata = {
                **base_metadata,
                "chunk_index": i,
                "total_chunks": len(chunks),
            }
            result.append({"content": chunk, "metadata": metadata})

        return result


# 全局实例
document_chunker = DocumentChunker()
