"""会议 API 路由"""

import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_meeting_or_404
from app.models.meeting import Meeting
from app.schemas.meeting import (
    MeetingCreate,
    MeetingUpdate,
    MeetingResponse,
    TranscriptResponse,
    TranscriptionStatusResponse,
)
from app.services.meeting_service import meeting_service
from app.services.transcription_service import transcription_service
from app.config import settings

router = APIRouter(prefix="/meetings", tags=["会议管理"])

ALLOWED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm"}


@router.post("", response_model=MeetingResponse, status_code=201)
async def create_meeting(
    data: MeetingCreate, db: AsyncSession = Depends(get_db)
) -> Meeting:
    """创建会议"""
    return await meeting_service.create_meeting(db, data)


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[Meeting]:
    """获取会议列表"""
    skip = (page - 1) * page_size
    meetings, _ = await meeting_service.list_meetings(db, skip=skip, limit=page_size)
    return meetings


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(meeting: Meeting = Depends(get_meeting_or_404)) -> Meeting:
    """获取会议详情"""
    return meeting


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    data: MeetingUpdate,
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> Meeting:
    """更新会议"""
    updated = await meeting_service.update_meeting(db, meeting.id, data)
    return updated  # type: ignore[return-value]


@router.delete("/{meeting_id}", status_code=204)
async def delete_meeting(
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> None:
    """删除会议"""
    await meeting_service.delete_meeting(db, meeting.id)


@router.post("/{meeting_id}/upload", response_model=MeetingResponse)
async def upload_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> Meeting:
    """上传音频文件并触发转写"""
    filename = file.filename or "audio.wav"
    if Path(filename).suffix.lower() not in ALLOWED_AUDIO_SUFFIXES:
        raise HTTPException(status_code=415, detail="不支持的音频文件格式")

    # UploadFile 使用临时文件承接请求；只读取上限再加 1 字节，避免把超大
    # 请求完整载入内存后才判断大小。
    file_content = await file.read(settings.MAX_UPLOAD_SIZE + 1)
    if not file_content:
        raise HTTPException(status_code=400, detail="音频文件为空")
    if len(file_content) > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="音频文件过大")

    updated = await meeting_service.save_audio(db, meeting.id, file_content, filename)

    # 后台触发转写（不阻塞响应）
    background_tasks.add_task(
        transcription_service.transcribe_and_store,
        str(meeting.id),
        updated.audio_url,  # type: ignore[arg-type]
    )
    return updated


@router.get("/{meeting_id}/audio")
async def get_audio(meeting: Meeting = Depends(get_meeting_or_404)):
    """获取音频文件"""
    if not meeting.audio_url or not os.path.exists(meeting.audio_url):
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(
        meeting.audio_url,
        media_type="audio/mpeg",
        filename=os.path.basename(meeting.audio_url),
    )


@router.get("/{meeting_id}/transcripts", response_model=list[TranscriptResponse])
async def get_transcripts(
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> list:
    """获取会议转写记录"""
    return await meeting_service.get_transcripts(db, meeting.id)


@router.get("/{meeting_id}/transcription-status", response_model=TranscriptionStatusResponse)
async def get_transcription_status(
    meeting: Meeting = Depends(get_meeting_or_404),
    db: AsyncSession = Depends(get_db),
) -> TranscriptionStatusResponse:
    """获取转写状态"""
    count = await meeting_service.get_transcript_count(db, meeting.id)
    return TranscriptionStatusResponse(
        meeting_id=meeting.id,
        status=meeting.status,
        transcript_count=count,
    )
