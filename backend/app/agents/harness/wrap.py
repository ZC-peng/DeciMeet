"""harness_wrap：节点装饰器，零侵入包裹现有 Agent

一个装饰器同时挂载：
1. BudgetGuard：Token / 成本计量
2. CircuitBreaker：熔断保护
3. with_smart_retry：错误分类重试
4. OutputValidator：输出结构校验
5. AgentRun step 记录：节点耗时 / 状态 / 错误

现有 Agent 节点无需修改，直接 @harness_wrap 即可升级。

agent_run_id 和 budget_guard 通过 contextvar 传递（不放入 langgraph state，
避免被 langgraph 序列化/过滤）。
"""

import asyncio
import contextvars
import functools
import logging
import time
from typing import Awaitable, Callable, Optional

from app.agents.harness.budget import BudgetGuard, BudgetExceededError
from app.agents.harness.circuit_breaker import llm_breaker
from app.agents.harness.retry import classify_error
from app.agents.harness.validator import validate_agent_output

logger = logging.getLogger(__name__)


# ── contextvar：跨节点传递 Harness 上下文 ──
# 不放入 langgraph state，因为 BudgetGuard 是 mutable 对象，
# 且 langgraph 对 TypedDict schema 之外的字段会过滤。
_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("harness_run_id", default="")
_budget_var: contextvars.ContextVar[Optional[BudgetGuard]] = contextvars.ContextVar(
    "harness_budget", default=None
)


def set_harness_context(run_id: str, budget: Optional[BudgetGuard]) -> None:
    """在调用图之前设置 Harness 上下文（由 summary_service 调用）"""
    _run_id_var.set(run_id)
    _budget_var.set(budget)


def get_run_id() -> str:
    return _run_id_var.get()


def get_budget() -> Optional[BudgetGuard]:
    return _budget_var.get()


def harness_wrap(
    node_name: Optional[str] = None,
    timeout: float = 60.0,
    validate_output: bool = True,
):
    """装饰器：为 Agent 节点套上 Harness 约束

    Args:
        node_name: 节点名（默认用函数名）
        timeout: 单次节点执行超时
        validate_output: 是否校验输出结构

    用法：
        @harness_wrap(node_name="summary_agent")
        async def summary_agent(state):
            ...
    """

    def decorator(func: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
        name = node_name or func.__name__

        @functools.wraps(func)
        async def wrapper(state: dict) -> dict:
            # 从 contextvar 读取（不依赖 langgraph state 传递）
            run_id = _run_id_var.get()
            budget = _budget_var.get()
            step_started_at = time.time()
            logger.info(f"[Harness] >>> 进入节点 {name} run_id={run_id[:8]} has_budget={budget is not None}")

            # 记录 step 开始
            await _record_step_start(run_id, name)

            # 熔断检查
            if not llm_breaker.allow():
                err = f"熔断中（{llm_breaker.state}），跳过节点 {name}"
                logger.warning(f"[Harness] {err}")
                await _record_step_end(run_id, name, "skipped", step_started_at, error=err)
                if _get_output_field(name):
                    return _recoverable_failure(name, err)
                return {"errors": [err]}

            # 执行节点（带超时）
            try:
                result = await asyncio.wait_for(
                    func(state),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                err = f"节点 {name} 超时（{timeout}s）"
                llm_breaker.record_failure(err)
                logger.error(f"[Harness] {err}")
                await _record_step_end(run_id, name, "timeout", step_started_at, error=err)
                if _get_output_field(name):
                    return _recoverable_failure(name, err)
                return {"errors": [err]}
            except BudgetExceededError as e:
                # 预算超限：不重试，直接终止
                logger.warning(f"[Harness] 预算超限 node={name}: {e}")
                await _record_step_end(run_id, name, "budget_exceeded", step_started_at, error=str(e))
                return {"errors": [str(e)], "budget_exceeded": True}
            except Exception as e:
                err_type = classify_error(e)
                llm_breaker.record_failure(str(e))
                err_msg = f"节点 {name} 执行失败 ({err_type}): {e}"
                logger.error(f"[Harness] {err_msg}")
                await _record_step_end(run_id, name, "failed", step_started_at, error=err_msg)
                if _get_output_field(name):
                    return _recoverable_failure(name, err_msg)
                return {"errors": [err_msg]}

            if not isinstance(result, dict):
                err = f"节点 {name} 返回值应为 dict，实际为 {type(result).__name__}"
                llm_breaker.record_failure(err)
                await _record_step_end(
                    run_id, name, "invalid_output", step_started_at, error=err
                )
                if _get_output_field(name):
                    return _recoverable_failure(name, err)
                return {"errors": [err]}

            # 基础 Agent 会保留 V1 使用的 errors，同时写入节点独立的可恢复错误。
            # 在 V2 中先移除这类中间 errors，避免 operator.add 让一次失败永久污染终态。
            node_error_field = _get_error_field(name)
            node_error = result.get(node_error_field) if node_error_field else None
            if node_error:
                llm_breaker.record_failure(str(node_error))
                logger.warning(f"[Harness] 节点失败可回灌 node={name}: {node_error}")
                await _record_step_end(
                    run_id,
                    name,
                    "invalid_output",
                    step_started_at,
                    error=str(node_error),
                )
                recoverable = dict(result)
                recoverable.pop("errors", None)
                return recoverable

            # 输出校验
            if validate_output:
                # 从 result 中取该节点产出的字段
                output_field = _get_output_field(name)
                raw_output = result.get(output_field) if output_field else None

                if output_field:
                    ok, msg, cleaned = await validate_agent_output(name, raw_output)
                    if not ok:
                        llm_breaker.record_failure(msg)
                        logger.warning(f"[Harness] 输出校验失败 node={name}: {msg}")
                        await _record_step_end(
                            run_id, name, "invalid_output",
                            step_started_at, error=msg,
                        )
                        recoverable = dict(result)
                        recoverable.pop("errors", None)
                        recoverable[node_error_field] = msg
                        return recoverable
                    # 用校验后的数据替换
                    result[output_field] = cleaned

            # 只有节点执行和输出校验都成功，才重置熔断器。
            llm_breaker.record_success()

            # 记录 step 成功（Token 已由 _invoke_with_retry 累计到 budget）
            # 把 budget 当前累计值同步到 AgentRun 数据库（持久化）
            if budget and run_id:
                try:
                    from app.services.agent_run_service import agent_run_service
                    await agent_run_service.update_budget(
                        run_id=run_id,
                        used_tokens=budget.used_tokens,
                        used_cost=budget.used_cost,
                        node_usage=budget.node_usage,
                    )
                except BudgetExceededError as e:
                    await _record_step_end(run_id, name, "budget_exceeded", step_started_at, error=str(e))
                    return {"errors": [str(e)], "budget_exceeded": True}
                except Exception as e:
                    logger.debug(f"同步 budget 到 AgentRun 失败（不影响执行）: {e}")

            await _record_step_end(run_id, name, "succeeded", step_started_at)
            return result

        # 标记为已包装（便于 graph 识别）
        wrapper._harness_wrapped = True  # type: ignore[attr-defined]
        wrapper._node_name = name  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ── 辅助函数 ──

_NODE_OUTPUT_FIELD = {
    "summary_agent": "summary",
    "action_items_agent": "action_items",
    "risks_agent": "risks",
    "decision_extractor": "decisions",
}


def _get_output_field(node_name: str) -> str:
    return _NODE_OUTPUT_FIELD.get(node_name, "")


def _get_error_field(node_name: str) -> str:
    """每个 fan-out 节点使用独立错误槽，避免并发更新同一普通字段。"""
    return f"{node_name}_error" if _get_output_field(node_name) else ""


def _recoverable_failure(node_name: str, error: str) -> dict:
    """构造可由 OutputValidator 回灌重试、但不会污染终态 errors 的失败结果。"""
    output_field = _get_output_field(node_name)
    if not output_field:
        return {"errors": [error]}
    return {
        output_field: None,
        _get_error_field(node_name): error,
    }


async def _record_step_start(run_id: str, node: str) -> None:
    """记录节点开始（持久化到 AgentRun.steps）"""
    if not run_id:
        return
    try:
        from app.services.agent_run_service import agent_run_service
        await agent_run_service.record_step_start(run_id, node)
    except Exception as e:
        logger.debug(f"记录 step start 失败（不影响执行）: {e}")


async def _record_step_end(
    run_id: str,
    node: str,
    status: str,
    started_at: float,
    error: Optional[str] = None,
) -> None:
    """记录节点结束（持久化到 AgentRun.steps）"""
    if not run_id:
        return
    duration_ms = int((time.time() - started_at) * 1000)
    try:
        from app.services.agent_run_service import agent_run_service
        await agent_run_service.record_step_end(
            run_id=run_id,
            node=node,
            status=status,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception as e:
        logger.debug(f"记录 step end 失败（不影响执行）: {e}")
