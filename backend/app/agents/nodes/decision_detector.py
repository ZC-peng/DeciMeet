"""DecisionDetector：两步流水线 Step 1 - 全量扫描定位决策段

使用 LLM 全量扫描与严格 Prompt 约束，明确区分决策、提议和推迟。

输入：带说话人标签的转写文本
输出：list[DecisionSegment]，只保留 type=decision 且 confidence >= 0.7
"""

import json
import logging
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.agents.harness.wrap import get_budget

logger = logging.getLogger(__name__)


class DecisionDetectionError(RuntimeError):
    """Detector 调用或结构化解析失败；与“确实没有决策”严格区分。"""


class DecisionSegment(BaseModel):
    """决策段：DecisionDetector 输出的最小单元"""

    snippet: str = Field(..., max_length=200, description="决策段原文（≤200 字）")
    type: Literal["decision", "proposal", "deferred"] = Field(
        ..., description="decision=已拍板 / proposal=提议未定 / deferred=推迟"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度")


DETECTOR_PROMPT = """你是评审会议决策识别专家。请扫描以下会议转写，找出所有「明确的决策」。

【决策的定义】
- 必须有最终拍板：「我们定...」「那就...」「拍板...」「一致同意...」
- 必须有具体方案：不是「下次再讨论」，而是「选 PostgreSQL」

【不算决策的情况】
- 提议但未拍板：「我觉得可以试试 X」 → type=proposal
- 推迟到下次：「这个下次再聊」 → type=deferred
- 单纯讨论：「X 的优点是...」 → 不输出

【输出 JSON】
{{
  "items": [
    {{"snippet": "决策段原文（≤200 字）", "type": "decision|proposal|deferred", "confidence": 0.0~1.0}}
  ]
}}

【转写文本】
{transcript}
"""


async def detect_decisions(
    transcript_text: str,
    retry_reason: str | None = None,
) -> list[DecisionSegment]:
    """全量扫描转写文本，定位所有决策段

    Args:
        transcript_text: 带说话人标签的完整转写文本

    Returns:
        过滤后的决策段列表（仅 type=decision 且 confidence >= 0.7）
    """
    if not transcript_text or not transcript_text.strip():
        return []

    # 长文本策略统一由上游 BudgetCheck 负责；这里不能再次截断，否则会漏掉尾部决策。
    transcript_for_prompt = transcript_text

    client = AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL
    )

    user_prompt = DETECTOR_PROMPT.format(transcript=transcript_for_prompt)
    if retry_reason:
        user_prompt += (
            "\n\n【上一次结构化输出失败】\n"
            f"{retry_reason}\n"
            "请修正 JSON 结构后重新输出。"
        )

    try:
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "你只输出 JSON 对象，不要任何解释、不要 markdown 代码块。",
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0.0,
            response_format={"type": "json_object"},  # 强制 JSON
        )
    except Exception as e:
        logger.error(f"[DecisionDetector] LLM 调用失败: {e}")
        raise DecisionDetectionError(f"LLM 调用失败: {e}") from e

    # 记录 Budget 消耗
    await _consume_budget("decision_detector", resp.usage)

    raw = resp.choices[0].message.content or ""
    segments, parse_error = _parse_segments_result(raw)
    if parse_error:
        logger.warning(f"[DecisionDetector] JSON 解析失败: {parse_error} | {raw[:200]}")
        raise DecisionDetectionError(f"JSON 解析失败: {parse_error}")
    if not segments:
        logger.info("[DecisionDetector] 模型返回合法空列表：未识别到候选决策段")
        return []

    # 过滤：只保留 decision 且 confidence >= 0.7
    filtered = [s for s in segments if s.type == "decision" and s.confidence >= 0.7]
    logger.info(
        f"[DecisionDetector] 识别 {len(segments)} 段，过滤后 {len(filtered)} 段"
    )
    return filtered


def _parse_segments(raw: str) -> list[DecisionSegment]:
    """宽松解析接口，保留给离线工具；执行路径使用带错误信息的严格版本。"""
    segments, _ = _parse_segments_result(raw)
    return segments


def _parse_segments_result(raw: str) -> tuple[list[DecisionSegment], str | None]:
    """解析 JSON，并区分合法空结果与解析/Schema 失败。"""
    if not raw or not raw.strip():
        return [], "响应为空"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], f"非法 JSON: {e.msg}"

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # 兼容多种 key
        matched_key = next(
            (key for key in ("items", "decisions", "segments") if key in data),
            None,
        )
        if matched_key is None:
            return [], "对象缺少 items/decisions/segments 字段"
        items = data[matched_key]
        if isinstance(items, dict):
            if "items" not in items:
                return [], f"{matched_key} 对象缺少 items 字段"
            items = items["items"]
    else:
        return [], f"顶层应为对象或数组，实际为 {type(data).__name__}"

    if not isinstance(items, list):
        return [], f"候选段应为数组，实际为 {type(items).__name__}"
    if not items:
        return [], None

    segments: list[DecisionSegment] = []
    invalid_count = 0
    for item in items:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        try:
            segments.append(DecisionSegment(**item))
        except ValidationError as e:
            invalid_count += 1
            logger.debug(f"[DecisionDetector] 跳过无效项: {item} - {e}")
    if not segments and invalid_count:
        return [], f"{invalid_count} 个候选段均未通过 Schema 校验"
    if invalid_count:
        logger.warning(f"[DecisionDetector] 跳过 {invalid_count} 个无效候选段")
    return segments, None


async def _consume_budget(node: str, usage) -> None:
    """记录 Budget 消耗（通过 contextvar 获取 BudgetGuard）

    BudgetExceededError 由 consume() 内部抛出，向上传播到 harness_wrap 处理。
    """
    budget = get_budget()
    if not budget or not usage:
        return
    await budget.consume(
        node,
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        model=settings.LLM_MODEL,
    )
