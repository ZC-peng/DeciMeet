"""知识库 API 路由"""

import os
import uuid
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import settings
from app.schemas.knowledge import (
    KnowledgeDocumentResponse,
    KnowledgeIndexRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    SearchResultItem,
)
from app.services.knowledge_service import knowledge_service

router = APIRouter(prefix="/knowledge", tags=["知识库"])
ALLOWED_DOCUMENT_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}


@router.post("/index", response_model=list[KnowledgeDocumentResponse])
async def index_text(data: KnowledgeIndexRequest, db: AsyncSession = Depends(get_db)):
    """索引文本到知识库"""
    docs = await knowledge_service.index_text(
        db=db,
        title=data.title,
        content=data.content,
        source_type=data.source_type,
        metadata=data.metadata,
    )
    return docs


@router.post("/upload", response_model=list[KnowledgeDocumentResponse])
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """上传文档并自动索引

    支持 PDF、Word、文本文件
    """
    filename = os.path.basename(
        (file.filename or "document.txt").replace("\\", "/")
    ) or "document.txt"
    if Path(filename).suffix.lower() not in ALLOWED_DOCUMENT_SUFFIXES:
        raise HTTPException(status_code=415, detail="仅支持 PDF、DOCX、TXT 和 Markdown")

    file_content = await file.read(settings.MAX_DOCUMENT_UPLOAD_SIZE + 1)
    if not file_content:
        raise HTTPException(status_code=400, detail="文档为空")

    # 检查文件大小
    if len(file_content) > settings.MAX_DOCUMENT_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大")

    # 保存临时文件
    temp_dir = os.path.join(settings.UPLOAD_DIR, "knowledge")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}_{filename}")

    def _write_temp_file() -> None:
        with open(temp_path, "wb") as f:
            f.write(file_content)

    await asyncio.to_thread(_write_temp_file)

    try:
        # 解析并索引
        docs = await knowledge_service.index_document_file(db, temp_path, filename)
        if docs is None:
            raise HTTPException(status_code=400, detail="无法解析文档，请检查文件格式")
        return docs
    finally:
        # 清理临时文件
        await asyncio.to_thread(Path(temp_path).unlink, missing_ok=True)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(data: KnowledgeSearchRequest, db: AsyncSession = Depends(get_db)):
    """知识库检索（混合检索 + Rerank）"""
    query = data.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query 不能为空")
    results = await knowledge_service.search(db, query, top_k=data.top_k)

    # 归一化后的 rerank_score 同步到 score 字段，
    # 让前端直接读 score 即可（最高分=1.0，其他按比例）
    for r in results:
        if r.get("rerank_score") is not None:
            r["score"] = r["rerank_score"]

    return KnowledgeSearchResponse(
        query=query,
        results=[SearchResultItem(**r) for r in results],
        total=len(results),
    )


@router.get("/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """获取知识文档列表"""
    skip = (page - 1) * page_size
    docs, _ = await knowledge_service.list_documents(db, skip=skip, limit=page_size)
    return docs


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """删除知识文档（按 title 删除所有相关 chunk）"""
    deleted = await knowledge_service.delete_document(db, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True, "message": "文档已删除"}
