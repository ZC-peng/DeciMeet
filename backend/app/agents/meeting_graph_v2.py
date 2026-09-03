"""Harness 升级版会议 Agent 工作流

基于现有 meeting_graph.py 升级，保留 v1 作对照。

升级点：
1. 引入 Planner 节点：分析会议类型/敏感度，动态调度 Agent
2. 套 Harness 约束：Budget / Retry / Breaker / Validator
3. Output Validator 节点：结构化校验 + 回灌重试
4. AgentRun 生命周期：记录节点状态、耗时、Token 与异常

流程：
    START
      ↓
    [planner] → 规划执行计划
      ↓
    [budget_check] → 文本压缩 + 预算检查
      ↓
    [summary_agent / action_items_agent / risks_agent]（动态 fan-out，套 Harness）
      ↓
    [output_validator] → 校验 + 回灌重试 → END

图执行完成后，由 SummaryService 在数据库事务中落库，避免图节点伪装持久化成功。
"""

import logging
import operator
from typing import Annotated, TypedDict, Optional

from langgraph.graph import StateGraph, END

from app.agents.meeting_graph import (
    summary_agent as _summary_agent,
    action_items_agent as _action_items_agent,
    risks_agent as _risks_agent,
)
from app.agents.harness import harness_wrap
from app.agents.nodes.planner import planner_node
from app.agents.nodes.budget_check import budget_check_node
from app.agents.nodes.output_validator import output_validator_node
from app.agents.nodes.decision_extractor import decision_extractor_node

logger = logging.getLogger(__name__)


# ── 升级版状态定义 ──

class MeetingAgentStateV2(TypedDict):
    """Harness 版 Agent 状态"""
    # 基础字段（兼容 v1）
    meeting_id: str
    meeting_title: str
    meeting_date: Optional[str]  # 会议开始时间（ISO 字符串），用于决策 decided_at
    transcript_text: str
    summary: str
    key_points: list[str]
    action_items: list[dict]
    risks: list[dict]
    errors: Annotated[list[str], operator.add]
    # 并行节点独立的可恢复错误；成功重试会以 None 覆盖。
    summary_agent_error: Optional[str]
    action_items_agent_error: Optional[str]
    risks_agent_error: Optional[str]
    decision_extractor_error: Optional[str]

    # Harness 新增字段
    # 注：agent_run_id / budget_guard 通过 contextvar 传递，不放入 state
    plan: dict
    transcript_compressed: bool
    # 节点重试计数
    summary_agent_retry: int
    action_items_agent_retry: int
    risks_agent_retry: int
    decision_extractor_retry: int
    # Validator 信号
    valid: bool
    retry_node: Optional[str]
    retry_reason: Optional[str]
    budget_exceeded: bool
    validation_failed: bool
    transcript_compressed_text: str
    # 评审决策抽取结果
    decisions: list[dict]
    decision_warnings: list[str]


# ── 用 harness_wrap 包裹现有 Agent（零侵入） ──

summary_agent_harnessed = harness_wrap(
    node_name="summary_agent",
    timeout=60.0,
)(_summary_agent)

action_items_agent_harnessed = harness_wrap(
    node_name="action_items_agent",
    timeout=60.0,
)(_action_items_agent)

risks_agent_harnessed = harness_wrap(
    node_name="risks_agent",
    timeout=60.0,
)(_risks_agent)

decision_extractor_harnessed = harness_wrap(
    node_name="decision_extractor",
    timeout=120.0,  # 两步流水线（detect + extract），给足时间
    validate_output=False,  # ExtractedDecision 内部已 Pydantic 校验
)(decision_extractor_node)

planner_harnessed = harness_wrap(
    node_name="planner",
    timeout=45.0,
    validate_output=False,
)(planner_node)

budget_check_harnessed = harness_wrap(
    node_name="budget_check",
    timeout=60.0,
    validate_output=False,
)(budget_check_node)

output_validator_harnessed = harness_wrap(
    node_name="output_validator",
    timeout=15.0,
    validate_output=False,
)(output_validator_node)


# ── 动态编排：根据 Planner 决定跑哪些 Agent ──

def route_after_budget_check(state) -> list[str]:
    """Planner 之后动态 fan-out 到选中的 Agent"""
    plan = state.get("plan", {})
    nodes = []
    if plan.get("should_run_summary", True):
        nodes.append("summary_agent")
    if plan.get("should_run_actions", True):
        nodes.append("action_items_agent")
    if plan.get("should_run_risks", True):
        nodes.append("risks_agent")
    if plan.get("should_run_decisions", True):
        nodes.append("decision_extractor")
    # 至少跑一个，兜底
    if not nodes:
        nodes.append("summary_agent")
    logger.info(f"[Router] 选中节点: {nodes}")
    return nodes


def route_after_validator(state) -> str:
    """Validator 之后：回灌失败节点，或结束图执行。"""
    # 需要回灌重试
    retry_node = state.get("retry_node")
    valid = state.get("valid", True)
    if not valid and retry_node:
        logger.info(f"[Router] validator → {retry_node} (retry)")
        return retry_node

    logger.info(
        f"[Router] validator → END (valid={valid}, retry_node={retry_node!r})"
    )
    return END


# ── 构建升级版图 ──

def build_harnessed_meeting_graph():
    """构建带 Harness 约束的会议 Agent 工作流"""
    workflow = StateGraph(MeetingAgentStateV2)

    # 添加节点
    workflow.add_node("planner", planner_harnessed)
    workflow.add_node("budget_check", budget_check_harnessed)
    workflow.add_node("summary_agent", summary_agent_harnessed)
    workflow.add_node("action_items_agent", action_items_agent_harnessed)
    workflow.add_node("risks_agent", risks_agent_harnessed)
    workflow.add_node("decision_extractor", decision_extractor_harnessed)
    workflow.add_node("output_validator", output_validator_harnessed)

    # 入口
    workflow.set_entry_point("planner")

    # planner → budget_check
    workflow.add_edge("planner", "budget_check")

    # budget_check → 动态 fan-out 到选中的 Agent
    workflow.add_conditional_edges("budget_check", route_after_budget_check)

    # 所有 Agent → output_validator（形成 Agent↔Validator 回灌循环）
    workflow.add_edge("summary_agent", "output_validator")
    workflow.add_edge("action_items_agent", "output_validator")
    workflow.add_edge("risks_agent", "output_validator")
    workflow.add_edge("decision_extractor", "output_validator")

    # output_validator → 动态路由（回灌重试到 retry_node / END）
    workflow.add_conditional_edges("output_validator", route_after_validator)

    return workflow.compile()


# 全局图实例
meeting_graph_v2 = build_harnessed_meeting_graph()
