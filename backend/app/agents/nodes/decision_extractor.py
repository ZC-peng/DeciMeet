"""decision_extractor 节点：组合 DecisionDetector + OptionExtractor 两步流水线

该节点与 summary、action_items、risks 并行接入 meeting_graph_v2。

失败先由 OutputValidator 回灌重试；只有重试耗尽才进入终态 errors。
"""

import logging

from app.agents.nodes.decision_detector import detect_decisions
from app.agents.nodes.option_extractor import extract_options
from app.agents.harness.budget import BudgetExceededError

logger = logging.getLogger(__name__)


async def decision_extractor_node(state: dict) -> dict:
    """评审决策抽取节点（两步流水线）

    流程：
        transcript_text
            ↓
        Step 1: detect_decisions (全量扫描定位决策段)
            ↓
        Step 2: extract_options (逐段抽取结构化选项)
            ↓
        list[dict] → state["decisions"]

    Returns:
        {"decisions": [...], "decision_extractor_error": None}；失败通过节点独立错误字段返回
    """
    # 只有 BudgetCheck 明确标记成功时才使用压缩文本，避免陈旧字段覆盖原文。
    compressed = state.get("transcript_compressed_text")
    if (
        state.get("transcript_compressed")
        and isinstance(compressed, str)
        and compressed.strip()
    ):
        transcript = compressed
    else:
        transcript = state.get("transcript_text", "")
    if not transcript or not transcript.strip():
        logger.warning("[decision_extractor] 无转写文本，跳过")
        return {
            "decisions": [],
            "decision_extractor_error": None,
            "decision_warnings": [],
        }

    retry_reason = None
    if state.get("retry_node") == "decision_extractor":
        retry_reason = state.get("retry_reason")

    try:
        # Step 1: 全量扫描定位决策段
        segments = await detect_decisions(transcript, retry_reason=retry_reason)
        if not segments:
            logger.info("[decision_extractor] 未识别到决策段")
            return {
                "decisions": [],
                "decision_extractor_error": None,
                "decision_warnings": [],
            }

        # Step 2: 逐段抽取结构化选项
        decisions: list[dict] = []
        warnings: list[str] = []
        # 从会议状态获取决策时间（parse ISO 字符串）
        from datetime import datetime
        meeting_date = None
        meeting_date_str = state.get("meeting_date")
        if meeting_date_str:
            try:
                meeting_date = datetime.fromisoformat(meeting_date_str)
            except (ValueError, TypeError):
                pass  # 解析失败则为 None

        for seg in segments:
            try:
                extracted = await extract_options(
                    seg,
                    transcript,
                    retry_reason=retry_reason,
                )
                decision_dict = extracted.model_dump(by_alias=True)
                # 保留 Detector 的证据与判定元数据，供持久化/引用/人工复核使用。
                decision_dict["snippet"] = seg.snippet
                decision_dict["confidence"] = seg.confidence
                decision_dict["decision_type"] = seg.type
                # 补充 decided_at（使用会议时间）
                if meeting_date:
                    decision_dict["decided_at"] = meeting_date
                decisions.append(decision_dict)
            except BudgetExceededError:
                raise
            except Exception as e:
                # 单个段抽取失败不影响其他段
                warning = f"snippet={seg.snippet[:50]!r}: {e}"
                warnings.append(warning)
                logger.warning(
                    f"[decision_extractor] 段抽取失败，跳过: {warning}"
                )

        logger.info(
            f"[decision_extractor] 识别 {len(segments)} 段，成功抽取 {len(decisions)} 个决策"
        )
        if warnings and not decisions:
            error = f"{len(segments)} 个决策段均抽取失败: {warnings[0]}"
            return {
                "decisions": [],
                "decision_extractor_error": error,
                "decision_warnings": warnings,
            }

        return {
            "decisions": decisions,
            "decision_extractor_error": None,
            "decision_warnings": warnings,
        }

    except BudgetExceededError:
        raise
    except Exception as e:
        # 使用节点独立错误槽交给 Validator 重试，避免一次瞬时失败污染终态。
        error = f"decision_extractor: {e}"
        logger.error(f"[decision_extractor] 失败，等待回灌重试: {e}")
        return {
            "decisions": [],
            "decision_extractor_error": error,
            "decision_warnings": [],
        }
