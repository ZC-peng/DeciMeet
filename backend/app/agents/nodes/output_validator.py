"""Output Validator 节点：结构化校验 + 回灌重试

校验 Planner 选中的专业 Agent 输出，失败时返回 retry_node 让工作流回灌重试该 Agent。
重试上限 2 次，超出则保留原始输出（兜底）并标记 errors。
"""

import logging

from app.agents.harness.validator import validate_agent_output

logger = logging.getLogger(__name__)

# 节点名 → 输出字段名
NODE_TO_FIELD = {
    "summary_agent": "summary",
    "action_items_agent": "action_items",
    "risks_agent": "risks",
    "decision_extractor": "decisions",
}

MAX_RETRY_PER_NODE = 2


async def output_validator_node(state: dict) -> dict:
    """输出校验节点

    返回值：
    - {"valid": True, ...cleaned_fields}：全部通过，回写校验清洗后的数据
    - {"valid": False, "retry_node": "xxx", "retry_reason": "xxx"}：回灌重试
    - {"valid": False, "errors": [...]}：重试耗尽，标记错误
    """
    errors = []
    cleaned_updates = {}

    # 预算超限是不可重试错误，避免回灌后继续消耗模型额度。
    if state.get("budget_exceeded"):
        return {
            "valid": False,
            "retry_node": None,
            "retry_reason": "预算已超限",
            "validation_failed": True,
        }

    for node_name, field_name in NODE_TO_FIELD.items():
        # 该节点是否在本次执行计划中
        plan = state.get("plan", {})
        if not _should_run_node(node_name, plan):
            continue

        raw = state.get(field_name)
        node_error = state.get(f"{node_name}_error")
        if node_error:
            ok, msg, cleaned = False, str(node_error), None
        else:
            # None 也是明确的失败，不能当作“节点没有结果”跳过。
            ok, msg, cleaned = await validate_agent_output(node_name, raw)
        if ok:
            # 校验通过：收集清洗后的数据，回写到 state
            cleaned_updates[field_name] = cleaned
            cleaned_updates[f"{node_name}_error"] = None
            continue

        # 校验失败：尝试回灌重试
        retry_count = state.get(f"{node_name}_retry", 0)
        if retry_count < MAX_RETRY_PER_NODE:
            logger.warning(
                f"[Validator] {node_name} 校验失败 (retry {retry_count + 1}/{MAX_RETRY_PER_NODE}): {msg}"
            )
            return {
                "valid": False,
                "retry_node": node_name,
                "retry_reason": msg,
                "validation_failed": True,
                f"{node_name}_retry": retry_count + 1,
            }

        # 重试耗尽：保留原始输出，标记错误
        logger.error(f"[Validator] {node_name} 重试耗尽，保留原始输出: {msg}")
        errors.append(f"{node_name} 输出校验失败（重试耗尽）: {msg}")

    if errors:
        # 显式清掉 retry_node，防止 router 误判为还要重试
        return {
            "valid": False,
            "errors": errors,
            "retry_node": None,
            "retry_reason": errors[0],
            "validation_failed": True,
        }

    return {
        "valid": True,
        "retry_node": None,
        "retry_reason": None,
        "validation_failed": False,
        **cleaned_updates,
    }


def _should_run_node(node_name: str, plan: dict) -> bool:
    """根据 Planner 计划判断该节点是否应该执行"""
    if not plan:
        return True
    if node_name == "summary_agent":
        return plan.get("should_run_summary", True)
    if node_name == "action_items_agent":
        return plan.get("should_run_actions", True)
    if node_name == "risks_agent":
        return plan.get("should_run_risks", True)
    if node_name == "decision_extractor":
        return plan.get("should_run_decisions", True)
    return True
