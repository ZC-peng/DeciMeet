"""AgentRun Token accounting semantics."""

from app.services.agent_run_service import AgentRunService


def test_node_usage_sums_input_and_output_tokens_separately() -> None:
    input_tokens, output_tokens = AgentRunService._summarize_node_usage(
        {
            "summary": {"input_tokens": 120, "output_tokens": 30},
            "risk": {"input_tokens": 80, "output_tokens": 20},
            "legacy": {"tokens": 999},
        }
    )

    assert input_tokens == 200
    assert output_tokens == 50
