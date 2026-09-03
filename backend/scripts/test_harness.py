#!/usr/bin/env python3
"""兼容入口：运行当前维护的 Agent Workflow 离线语义测试。"""

import unittest

from app.agents.tests import test_workflow_semantics


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_workflow_semantics)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
