"""不调用真实 LLM 的 Agent V2 核心语义测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _install_dependency_stubs() -> None:
    """让最小 CI 环境也能运行纯逻辑测试；完整环境会使用真实依赖。"""
    try:
        __import__("app.config")
    except ImportError:
        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(
            LLM_MODEL="test-model",
            OPENAI_API_KEY="test-key",
            OPENAI_BASE_URL="https://example.invalid/v1",
        )
        sys.modules["app.config"] = config

    if importlib.util.find_spec("langchain_openai") is None:
        module = types.ModuleType("langchain_openai")

        class ChatOpenAI:
            def __init__(self, *args, **kwargs):
                self.model_name = kwargs.get("model", "test-model")

        module.ChatOpenAI = ChatOpenAI
        sys.modules["langchain_openai"] = module

    if importlib.util.find_spec("langchain_core") is None:
        package = types.ModuleType("langchain_core")
        package.__path__ = []
        messages = types.ModuleType("langchain_core.messages")

        class _Message:
            def __init__(self, content: str):
                self.content = content

        messages.SystemMessage = _Message
        messages.HumanMessage = _Message
        sys.modules["langchain_core"] = package
        sys.modules["langchain_core.messages"] = messages

    if importlib.util.find_spec("langgraph") is None:
        package = types.ModuleType("langgraph")
        package.__path__ = []
        graph = types.ModuleType("langgraph.graph")

        class StateGraph:
            def __init__(self, *_args, **_kwargs):
                pass

            def add_node(self, *_args, **_kwargs):
                pass

            def set_entry_point(self, *_args, **_kwargs):
                pass

            def add_edge(self, *_args, **_kwargs):
                pass

            def add_conditional_edges(self, *_args, **_kwargs):
                pass

            def compile(self):
                return self

        graph.StateGraph = StateGraph
        graph.END = "__end__"
        sys.modules["langgraph"] = package
        sys.modules["langgraph.graph"] = graph

    if importlib.util.find_spec("openai") is None:
        module = types.ModuleType("openai")

        class AsyncOpenAI:
            def __init__(self, *_args, **_kwargs):
                pass

        module.AsyncOpenAI = AsyncOpenAI
        sys.modules["openai"] = module


_install_dependency_stubs()

from app.agents import meeting_graph
from app.agents.harness.budget import BudgetGuard
from app.agents.harness.circuit_breaker import llm_breaker
from app.agents.harness.wrap import harness_wrap, set_harness_context
from langgraph.graph import END

from app.agents.meeting_graph_v2 import route_after_budget_check, route_after_validator
from app.agents.nodes import decision_extractor as decision_node
from app.agents.nodes.decision_detector import (
    DecisionDetectionError,
    DecisionSegment,
    _parse_segments_result,
)
from app.agents.nodes.option_extractor import ExtractedDecision
from app.agents.nodes.output_validator import output_validator_node


class AgentPromptSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_text_and_retry_reason_are_fed_back(self) -> None:
        invoke = AsyncMock(return_value=("[]", None))
        state = {
            "meeting_title": "评审会",
            "transcript_text": "ORIGINAL_TRANSCRIPT",
            "transcript_compressed": True,
            "transcript_compressed_text": "COMPRESSED_TRANSCRIPT",
            "retry_node": "action_items_agent",
            "retry_reason": "priority 字段非法",
        }

        with patch.object(meeting_graph, "get_llm", return_value=object()), patch.object(
            meeting_graph, "_invoke_with_retry", invoke
        ):
            result = await meeting_graph.action_items_agent(state)

        messages = invoke.await_args.args[1]
        user_prompt = messages[1].content
        self.assertIn("COMPRESSED_TRANSCRIPT", user_prompt)
        self.assertNotIn("ORIGINAL_TRANSCRIPT", user_prompt)
        self.assertIn("priority 字段非法", user_prompt)
        self.assertEqual(invoke.await_args.kwargs["node_name"], "action_items_agent")
        self.assertEqual(result["action_items"], [])
        self.assertIsNone(result["action_items_agent_error"])

    async def test_malformed_json_is_not_a_valid_empty_result(self) -> None:
        with patch.object(meeting_graph, "get_llm", return_value=object()), patch.object(
            meeting_graph,
            "_invoke_with_retry",
            AsyncMock(return_value=("not-json", None)),
        ):
            result = await meeting_graph.action_items_agent(
                {"meeting_title": "会", "transcript_text": "内容"}
            )

        self.assertEqual(result["action_items"], [])
        self.assertIn("JSON 数组", result["action_items_agent_error"])
        self.assertTrue(result["errors"])


class HarnessAndValidatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        llm_breaker.state = "closed"
        llm_breaker.fail_count = 0
        llm_breaker.last_error = None
        set_harness_context("", None)

    async def test_intermediate_error_is_stripped_and_successful_retry_clears_it(self) -> None:
        async def malformed(_state):
            return {
                "action_items": [],
                "action_items_agent_error": "非法 JSON",
                "errors": ["非法 JSON"],
            }

        wrapped = harness_wrap(node_name="action_items_agent")(malformed)
        intermediate = await wrapped({})
        self.assertNotIn("errors", intermediate)
        self.assertEqual(intermediate["action_items_agent_error"], "非法 JSON")

        plan = {
            "should_run_summary": False,
            "should_run_actions": True,
            "should_run_risks": False,
            "should_run_decisions": False,
        }
        retry = await output_validator_node({**intermediate, "plan": plan})
        self.assertFalse(retry["valid"])
        self.assertEqual(retry["retry_node"], "action_items_agent")
        self.assertNotIn("errors", retry)

        recovered = await output_validator_node(
            {
                "plan": plan,
                "action_items": [
                    {
                        "title": "修复问题",
                        "assignee": None,
                        "due_date": None,
                        "priority": "high",
                    }
                ],
                "action_items_agent_error": None,
                "action_items_agent_retry": 1,
                # 模拟 state 中没有任何旧的终态错误。
                "errors": [],
            }
        )
        self.assertTrue(recovered["valid"])
        self.assertIsNone(recovered["retry_node"])
        self.assertIsNone(recovered["action_items_agent_error"])
        self.assertNotIn("errors", recovered)

    async def test_retry_exhaustion_creates_terminal_error_once(self) -> None:
        result = await output_validator_node(
            {
                "plan": {
                    "should_run_summary": False,
                    "should_run_actions": True,
                    "should_run_risks": False,
                    "should_run_decisions": False,
                },
                "action_items": [],
                "action_items_agent_error": "仍是非法 JSON",
                "action_items_agent_retry": 2,
            }
        )
        self.assertFalse(result["valid"])
        self.assertIsNone(result["retry_node"])
        self.assertEqual(len(result["errors"]), 1)


class PlannerAndDecisionTests(unittest.IsolatedAsyncioTestCase):
    def test_planner_result_drives_real_fan_out(self) -> None:
        nodes = route_after_budget_check(
            {
                "plan": {
                    "should_run_summary": False,
                    "should_run_actions": True,
                    "should_run_risks": False,
                    "should_run_decisions": True,
                }
            }
        )
        self.assertEqual(nodes, ["action_items_agent", "decision_extractor"])

    def test_validated_result_ends_graph_before_service_persistence(self) -> None:
        self.assertEqual(
            route_after_validator({"valid": True, "retry_node": None}),
            END,
        )

    def test_detector_distinguishes_valid_empty_from_parse_failure(self) -> None:
        segments, error = _parse_segments_result('{"items": []}')
        self.assertEqual(segments, [])
        self.assertIsNone(error)

        segments, error = _parse_segments_result("not-json")
        self.assertEqual(segments, [])
        self.assertIn("非法 JSON", error)

    async def test_two_stage_decision_keeps_detector_evidence(self) -> None:
        segment = DecisionSegment(
            snippet="张三：那就采用 PostgreSQL。",
            type="decision",
            confidence=0.93,
        )
        extracted = ExtractedDecision.model_validate(
            {
                "title": "采用 PostgreSQL",
                "context": "数据库选型评审",
                "options": [{"name": "PostgreSQL"}],
                "chosen": "PostgreSQL",
                "reasons": ["支持复杂查询"],
            }
        )

        with patch.object(
            decision_node, "detect_decisions", AsyncMock(return_value=[segment])
        ), patch.object(
            decision_node, "extract_options", AsyncMock(return_value=extracted)
        ):
            result = await decision_node.decision_extractor_node(
                {"transcript_text": "张三：那就采用 PostgreSQL。"}
            )

        decision = result["decisions"][0]
        self.assertEqual(decision["snippet"], segment.snippet)
        self.assertEqual(decision["confidence"], 0.93)
        self.assertEqual(decision["decision_type"], "decision")
        self.assertIsNone(result["decision_extractor_error"])

    async def test_detector_failure_is_not_reported_as_no_decision(self) -> None:
        with patch.object(
            decision_node,
            "detect_decisions",
            AsyncMock(side_effect=DecisionDetectionError("invalid JSON")),
        ):
            result = await decision_node.decision_extractor_node(
                {"transcript_text": "存在决策的转写"}
            )

        self.assertEqual(result["decisions"], [])
        self.assertIn("invalid JSON", result["decision_extractor_error"])
        self.assertNotIn("errors", result)


class BudgetUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_output_total_are_kept_separately_without_fake_cost(self) -> None:
        guard = BudgetGuard(run_id="", max_tokens=100, max_cost_usd=0.5)
        await guard.consume("summary_agent", tokens_in=12, tokens_out=3)

        self.assertEqual(guard.input_tokens, 12)
        self.assertEqual(guard.output_tokens, 3)
        self.assertEqual(guard.used_tokens, 15)
        self.assertEqual(guard.used_cost_usd, 0.0)
        self.assertEqual(guard.node_usage["summary_agent"]["input_tokens"], 12)
        self.assertEqual(guard.node_usage["summary_agent"]["output_tokens"], 3)
        self.assertEqual(guard.node_usage["summary_agent"]["total_tokens"], 15)
        self.assertEqual(
            guard.summary()["cost_tracking"], "disabled_unverified_pricing"
        )

    async def test_langchain_usage_metadata_is_consumed_by_node(self) -> None:
        guard = BudgetGuard(run_id="", max_tokens=100)
        set_harness_context("", guard)
        llm = SimpleNamespace(model_name="test-model")
        response = SimpleNamespace(
            usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        )

        await meeting_graph._try_consume_budget(
            llm, response, node_name="risks_agent"
        )

        self.assertEqual(guard.input_tokens, 7)
        self.assertEqual(guard.output_tokens, 2)
        self.assertEqual(guard.used_tokens, 9)
        self.assertIn("risks_agent", guard.node_usage)


if __name__ == "__main__":
    unittest.main()
