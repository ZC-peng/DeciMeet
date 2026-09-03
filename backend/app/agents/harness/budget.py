"""Budget Guard：Token / 成本双闸门

四道闸门：
1. 输入长度：转写文本 > 阈值时由 Planner 决定压缩策略
2. 单次 Token：LLM max_tokens 硬限制
3. 单会议总量：总 Token > max_tokens 则中止
4. 成本字段统一使用 USD；未配置可信 USD 价格表时明确不估算成本
"""

import logging
from dataclasses import InitVar, dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 当前没有来源、版本和生效时间均可核验的 USD 价格配置。
# 因此只记录真实 Token，不把原先的 CNY 数值写入 total_cost_usd。
# 后续若接入价格配置中心，可按模型版本填充 {input, output}（USD / 1K tokens）。
MODEL_PRICING_USD: dict[str, dict[str, float]] = {}


class BudgetExceededError(Exception):
    """预算超限异常"""

    def __init__(self, kind: str, used: float, limit: float):
        self.kind = kind  # "tokens" / "cost"
        self.used = used
        self.limit = limit
        super().__init__(f"Budget {kind} exceeded: {used} > {limit}")


@dataclass
class BudgetGuard:
    """单次 Agent Run 的预算管理器

    使用方式：
        guard = BudgetGuard(run_id=run_id)
        await guard.consume("summary_agent", tokens_in=1200, tokens_out=800, model="qwen-plus")
    """

    run_id: str
    max_tokens: int = 50000
    max_cost_usd: Optional[float] = 0.5
    # 兼容旧调用方；其数值按 USD 限额解释，不参与任何 CNY 换算。
    max_cost_cny: InitVar[Optional[float]] = None
    input_tokens: int = 0
    output_tokens: int = 0
    used_tokens: int = 0
    used_cost_usd: float = 0.0
    # 节点级明细保留 tokens/cost 兼容键，并提供明确的拆分字段。
    node_usage: dict = field(default_factory=dict)

    def __post_init__(self, max_cost_cny: Optional[float]) -> None:
        if max_cost_cny is not None:
            # summary_service 的旧参数名实际对应 AgentRun.max_cost_usd。
            self.max_cost_usd = max_cost_cny
            logger.warning(
                "BudgetGuard.max_cost_cny 已废弃；该数值按 USD 限额解释，"
                "请迁移到 max_cost_usd"
            )

    @property
    def used_cost(self) -> float:
        """兼容上层旧字段名；值始终表示 USD。"""
        return self.used_cost_usd

    async def consume(
        self,
        node: str,
        tokens_in: int,
        tokens_out: int,
        model: str = None,
    ) -> None:
        """记录一次 LLM 调用的 Token 与成本消耗

        超限时抛出 BudgetExceededError，由 harness_wrap 捕获并标记节点失败。
        """
        model = model or settings.LLM_MODEL
        tokens_in = max(0, int(tokens_in or 0))
        tokens_out = max(0, int(tokens_out or 0))
        pricing = MODEL_PRICING_USD.get(model)
        cost_usd = None
        if pricing:
            cost_usd = (
                tokens_in * pricing["input"] + tokens_out * pricing["output"]
            ) / 1000

        new_tokens = self.used_tokens + tokens_in + tokens_out
        new_cost_usd = self.used_cost_usd + (cost_usd or 0.0)

        # 闸门 1：Token 总量
        if new_tokens > self.max_tokens:
            logger.warning(
                f"[BudgetGuard] Token 超限 run={self.run_id} node={node} "
                f"{new_tokens} > {self.max_tokens}"
            )
            raise BudgetExceededError("tokens", new_tokens, self.max_tokens)

        # 闸门 2：仅在有可信 USD 价格时启用成本限制。
        if (
            cost_usd is not None
            and self.max_cost_usd is not None
            and new_cost_usd > self.max_cost_usd
        ):
            logger.warning(
                f"[BudgetGuard] 成本超限 run={self.run_id} node={node} "
                f"${new_cost_usd:.4f} > ${self.max_cost_usd}"
            )
            raise BudgetExceededError("cost_usd", new_cost_usd, self.max_cost_usd)

        # 提交
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out
        self.used_tokens = new_tokens
        self.used_cost_usd = new_cost_usd
        node_stat = self.node_usage.setdefault(
            node,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "tokens": 0,
                "cost_usd": 0.0,
                "cost": 0.0,
            },
        )
        node_stat["input_tokens"] += tokens_in
        node_stat["output_tokens"] += tokens_out
        node_stat["total_tokens"] += tokens_in + tokens_out
        node_stat["tokens"] += tokens_in + tokens_out
        if cost_usd is not None:
            node_stat["cost_usd"] += cost_usd
            node_stat["cost"] += cost_usd

        # 持久化到 AgentRun（延迟导入避免循环依赖）；离线测试没有 run_id 时跳过。
        if self.run_id:
            try:
                from app.services.agent_run_service import agent_run_service
                await agent_run_service.update_budget(
                    run_id=self.run_id,
                    used_tokens=self.used_tokens,
                    used_cost=self.used_cost_usd,
                    node_usage=self.node_usage,
                )
            except Exception as e:
                logger.debug(f"BudgetGuard 持久化失败（不影响执行）: {e}")

    def summary(self) -> dict:
        """返回预算汇总（供 AgentRun 记录）"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.used_tokens,
            "used_tokens": self.used_tokens,
            "used_cost_usd": round(self.used_cost_usd, 6),
            "used_cost": round(self.used_cost_usd, 6),
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "cost_tracking": "enabled" if MODEL_PRICING_USD else "disabled_unverified_pricing",
            "node_usage": self.node_usage,
        }
