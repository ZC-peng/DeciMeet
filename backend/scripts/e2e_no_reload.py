#!/usr/bin/env python3
"""兼容入口：不启动开发服务器，直接运行数据库级端到端演示。

该脚本与 ``e2e_simple.py`` 使用同一条受维护链路，避免多个演示脚本产生行为漂移。
"""

from e2e_simple import main


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
