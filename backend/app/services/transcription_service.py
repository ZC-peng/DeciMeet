"""音频转写服务（Provider 抽象 + 自动降级）

转写链路（按 TRANSCRIPTION_PROVIDER 配置）：
- auto（默认）：DashScope 原生 ASR + OSS → 失败降级 Mock
- dashscope：仅 DashScope 原生 ASR（失败抛错，不降级）
- mock：仅 Mock（本地演示用）

真实转写流程（DashScope 原生 ASR）：
1. 本地文件 → 上传 OSS → 获取公网 URL
2. 提交 DashScope 录音识别任务（异步）
3. 轮询任务状态直到 SUCCEEDED
4. 解析带时间戳和说话人的转写片段
5. 清理 OSS 上的临时文件

Mock 降级场景：
- 未配置 OSS 或 DashScope API Key
- 未安装 oss2 依赖
- DashScope API 调用失败（网络、额度、超时）
- TRANSCRIPTION_PROVIDER=mock
"""

import logging
import os
import asyncio
import uuid
from typing import Optional

from app.config import settings
from app.services.dashscope_asr_service import dashscope_asr_service
from app.services.oss_service import oss_service

logger = logging.getLogger(__name__)


# ============================================================
# Mock 转写语料（用于无 ASR 凭证时演示完整决策抽取链路）
# ============================================================
_MOCK_SEGMENTS = [
    ("林悦", "今天评审会议助手 V1，先确定流式协议和知识库方案。"),
    ("周航", "流式输出我提议用 WebSocket，以后如果加入语音控制可以复用双向连接。"),
    ("陈墨", "当前只是服务端向浏览器推送模型增量，我建议用 SSE，协议更简单，也支持自动重连。"),
    ("赵宁", "WebSocket 的灵活性更高，但我们还要自己处理心跳、重连和消息格式，测试面会更大。"),
    ("周航", "我保留意见：如果下个版本做实时双向控制，SSE 可能需要再迁移。"),
    ("林悦", "这次拍板使用 POST SSE。V1 是单向流式回答，先控制实现复杂度；双向控制等需求确认后再评估。"),
    ("林悦", "第二项是 RAG 存储，候选是 PostgreSQL 加 pgvector，或者单独部署向量数据库。"),
    ("陈墨", "我提议用 PostgreSQL 加 pgvector，会议、文档和向量能放在同一事务边界，部署也只有一个数据库。"),
    ("周航", "独立向量库在大规模检索上扩展更好，但当前数据量不大，引入新服务会增加运维成本。"),
    ("赵宁", "全文检索也要保留，专有名词和精确编号只靠向量召回不稳定。"),
    ("林悦", "决定 V1 使用 PostgreSQL、pgvector 和全文检索做混合召回；数据达到百万分片后再评估独立向量库。"),
    ("陈墨", "检索流程先做向量和全文双路召回，用 RRF 融合，再做关键词重排和相似分片去重。"),
    ("林悦", "陈墨负责后端检索，截止 2026-09-08；周航负责 SSE 时间线展示，截止 2026-09-09。"),
    ("赵宁", "我在 2026-09-10 前补充 JSON 解析失败、ASR 超时和重复上传的回归用例，优先级高。"),
    ("赵宁", "目前最大的风险是 ASR 额度或 OSS 配置缺失，可能导致演示链路中断。"),
    ("陈墨", "本地演示提供明确标记的 Mock Provider，真实模式失败时记录 failed，auto 模式才允许降级。"),
    ("周航", "另一个风险是长转写导致页面节点过多，前端使用动态高度虚拟列表控制渲染数量。"),
    ("林悦", "结论就这两项：SSE 和 PostgreSQL 混合检索。延期项与反对意见都保留到决策档案。"),
]


class TranscriptionService:
    """音频转写服务

    自动在 DashScope 真实转写与 Mock 之间切换，返回 (segments, mode)。
    mode: 'real' / 'mock'
    """

    def __init__(self):
        self.provider = settings.TRANSCRIPTION_PROVIDER

    @staticmethod
    def _normalize_segments(segments: list[dict]) -> list[dict]:
        """Validate provider output and assign one deterministic sequence.

        Provider sequence identifiers are not trusted as database keys: some ASR
        APIs omit them or repeat them after speaker diarization.  Ordering is
        retained where possible, while blank segments are discarded.
        """
        sortable: list[tuple[tuple[float, int], dict]] = []
        for position, raw in enumerate(segments):
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                continue

            try:
                order = float(raw.get("seq_index", position))
            except (TypeError, ValueError):
                order = float(position)

            def optional_float(value):
                try:
                    return float(value) if value is not None else None
                except (TypeError, ValueError):
                    return None

            start_time = optional_float(raw.get("start_time"))
            end_time = optional_float(raw.get("end_time"))
            if start_time is not None:
                start_time = max(0.0, start_time)
            if end_time is not None:
                end_time = max(start_time or 0.0, end_time)

            speaker = str(raw.get("speaker") or "").strip() or None
            sortable.append(
                (
                    (order, position),
                    {
                        "speaker": speaker[:100] if speaker else None,
                        "content": content,
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                )
            )

        normalized = []
        for seq_index, (_, segment) in enumerate(sorted(sortable, key=lambda x: x[0])):
            normalized.append({**segment, "seq_index": seq_index})
        return normalized

    @staticmethod
    async def _mark_failed(meeting_id: uuid.UUID, audio_path: str) -> None:
        """Mark only the still-current upload as failed."""
        from app.db.session import async_session_factory
        from app.models.meeting import Meeting
        from sqlalchemy import update

        async with async_session_factory() as session:
            await session.execute(
                update(Meeting)
                .where(
                    Meeting.id == meeting_id,
                    Meeting.audio_url == audio_path,
                )
                .values(status="failed", transcription_mode=None)
            )
            await session.commit()

    async def transcribe(
        self, audio_path: str, language: str = "zh"
    ) -> tuple[Optional[list[dict]], str]:
        """
        转写音频文件

        Returns:
            (segments, mode)
            - segments: 转写片段列表；None 表示彻底无法转写
            - mode: 'real'(真实API) / 'mock'(本地降级)
        """
        # 前置检查：音频路径必须存在
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"音频文件不存在: {audio_path!r}")
            return None, "mock"

        provider = self.provider

        # 强制 Mock 模式
        if provider == "mock":
            logger.info("TRANSCRIPTION_PROVIDER=mock，使用 Mock 转写")
            return await self._mock_transcribe(audio_path), "mock"

        # auto / dashscope：尝试真实转写
        if provider in ("auto", "dashscope"):
            segments = await self._transcribe_via_dashscope(audio_path, language)
            if segments is not None:
                return segments, "real"

            # dashscope 模式不降级
            if provider == "dashscope":
                logger.error("dashscope 模式转写失败，不降级")
                return None, "real"

            # auto 模式降级到 Mock
            logger.warning("真实转写失败，降级到 Mock")
            return await self._mock_transcribe(audio_path), "mock"

        # 未知 provider
        logger.warning(f"未知 TRANSCRIPTION_PROVIDER={provider}，使用 Mock")
        return await self._mock_transcribe(audio_path), "mock"

    async def _transcribe_via_dashscope(
        self, audio_path: str, language: str
    ) -> Optional[list[dict]]:
        """
        通过 DashScope 原生 ASR + OSS 真实转写

        流程：本地文件 → OSS → 公网 URL → DashScope ASR → 解析结果 → 清理 OSS
        """
        # 1. 检查前置条件
        if not dashscope_asr_service.is_available:
            logger.warning("DashScope API Key 未配置，跳过真实转写")
            return None

        if not oss_service.is_available:
            logger.warning("OSS 未配置或 oss2 未安装，跳过真实转写")
            return None

        # 2. 上传到 OSS 获取公网 URL
        audio_url = await asyncio.to_thread(oss_service.upload_audio, audio_path)
        if not audio_url:
            logger.error("上传 OSS 失败，跳过真实转写")
            return None

        # 3. 调用 DashScope ASR
        try:
            language_hints = [language] if language else ["zh", "en"]
            segments = await dashscope_asr_service.transcribe(
                audio_url, language_hints=language_hints
            )
            if segments:
                logger.info(f"DashScope 真实转写成功，共 {len(segments)} 个片段")
                return segments
            logger.error("DashScope ASR 返回空结果")
            return None
        except Exception as e:
            logger.error(f"DashScope ASR 调用异常: {e}")
            return None
        finally:
            # 4. 清理 OSS 临时文件（无论成功失败）
            await asyncio.to_thread(oss_service.delete_object, audio_url)

    async def _mock_transcribe(self, audio_path: str) -> list[dict]:
        """
        生成 Mock 转写结果
        - 根据音频文件大小估算时长，裁剪/循环语料到匹配长度
        - 每个片段分配合理的时间戳
        """
        # 估算音频时长：mp3 ~ 1MB/分钟（128kbps）
        try:
            file_size = os.path.getsize(audio_path)
        except OSError:
            file_size = 5 * 1024 * 1024  # 默认按 5 分钟算

        estimated_minutes = max(1, file_size / (1024 * 1024))
        estimated_duration = estimated_minutes * 60.0  # 秒

        # Mock 的目标是稳定演示完整的“提议 → 反对 → 拍板 → 行动项 → 风险”
        # 链路，因此不根据占位音频大小裁掉后半段关键语料。
        chosen = list(_MOCK_SEGMENTS)
        estimated_duration = max(estimated_duration, len(chosen) * 8.0)

        # 时间戳分配
        segments = []
        seg_duration = estimated_duration / len(chosen)
        for i, (speaker, content) in enumerate(chosen):
            start = i * seg_duration
            end = start + seg_duration - 0.5
            segments.append(
                {
                    "speaker": speaker,
                    "content": content,
                    "start_time": round(start, 2),
                    "end_time": round(max(start, end), 2),
                    "seq_index": i,
                }
            )
        logger.info(
            f"Mock 转写完成：{len(segments)} 个片段，估算时长 {estimated_minutes:.1f} 分钟"
        )
        return segments

    async def transcribe_and_store(
        self, meeting_id: str, audio_path: str
    ) -> tuple[bool, str]:
        """
        转写音频并存储到数据库

        Returns:
            (success, mode)
            - success: True 表示转写并存储成功
            - mode: 'real' / 'mock'
        """
        from app.db.session import async_session_factory
        from app.models.transcript import Transcript
        from app.models.meeting import Meeting
        from sqlalchemy import delete, select, update

        meeting_uuid = uuid.UUID(str(meeting_id))

        # 更新状态为转写中
        async with async_session_factory() as session:
            update_result = await session.execute(
                update(Meeting)
                .where(
                    Meeting.id == meeting_uuid,
                    Meeting.audio_url == audio_path,
                )
                .values(status="transcribing", transcription_mode=None)
            )
            await session.commit()
            if update_result.rowcount == 0:
                logger.warning(
                    "忽略不存在或已被新上传替换的转写任务：meeting=%s path=%s",
                    meeting_uuid,
                    audio_path,
                )
                return False, "real" if self.provider == "dashscope" else "mock"

        try:
            segments, mode = await self.transcribe(audio_path)
            if segments is None:
                await self._mark_failed(meeting_uuid, audio_path)
                return False, mode

            normalized_segments = self._normalize_segments(segments)
            if not normalized_segments:
                logger.error("ASR 返回结果不含有效文本片段")
                await self._mark_failed(meeting_uuid, audio_path)
                return False, mode

            # Lock and re-check the upload identity.  An older background task
            # must never replace transcripts for a newer audio upload.
            async with async_session_factory() as session:
                meeting_result = await session.execute(
                    select(Meeting)
                    .where(Meeting.id == meeting_uuid)
                    .with_for_update()
                )
                meeting = meeting_result.scalar_one_or_none()
                if not meeting or meeting.audio_url != audio_path:
                    logger.warning(
                        "转写完成时音频已变化，丢弃过期结果：meeting=%s",
                        meeting_uuid,
                    )
                    await session.rollback()
                    return False, mode

                # Replace the transcript snapshot in one transaction.  If any
                # insert fails, PostgreSQL rolls the delete back as well.
                await session.execute(
                    delete(Transcript).where(Transcript.meeting_id == meeting_uuid)
                )
                for seg in normalized_segments:
                    transcript = Transcript(
                        meeting_id=meeting_uuid,
                        speaker=seg["speaker"],
                        content=seg["content"],
                        start_time=seg["start_time"],
                        end_time=seg["end_time"],
                        seq_index=seg["seq_index"],
                    )
                    session.add(transcript)

                # 更新会议状态 + 记录转写模式
                meeting.status = "processed"
                meeting.transcription_mode = mode
                await session.commit()

            mode_label = "Mock" if mode == "mock" else "真实"
            logger.info(f"会议 {meeting_uuid} 转写结果已存储（{mode_label}转写）")
            return True, mode

        except Exception as e:
            logger.error(f"存储转写结果失败: {e}")
            await self._mark_failed(meeting_uuid, audio_path)
            raise


# 全局实例
transcription_service = TranscriptionService()
