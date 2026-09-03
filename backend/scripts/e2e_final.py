#!/usr/bin/env python3
"""兼容入口：运行当前维护的会议决策端到端演示。

前置条件见项目 README：PostgreSQL 已初始化，且已配置可用的 LLM/Embedding。
"""

from e2e_simple import main


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
