# -*- coding: utf-8 -*-
"""
Workers 模块 - 包含各种执行器和任务类
"""

# DEPRECATED: 旧版同步 Workers（使用 QThread/QRunnable）
# 这些类将在未来版本中移除，请迁移到对应的异步版本
from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.subagent_worker import SubAgentExecutor, SubAgentManager
from app.core.workers.topic_summary import TopicSummaryTask as SyncTopicSummaryTask
from app.core.workers.shell_task import ShellExecutionTask

# 新版异步 Workers（使用 threading）
from app.core.workers.async_chat_worker import AsyncOpenAIChatWorker
from app.core.workers.async_topic_summary import TopicSummaryTask
from app.core.workers.async_shell_task import execute_shell

# 注意: SubAgentExecutor 尚未提供异步版本，仍需使用同步版本
# 如需异步执行子智能体，请使用 task_batch 工具

__all__ = [
    # DEPRECATED - 同步版本（将在未来移除）
    "OpenAIChatWorker",
    "SubAgentExecutor",
    "SubAgentManager",
    "SyncTopicSummaryTask",
    "ShellExecutionTask",
    # 异步版本（推荐使用）
    "AsyncOpenAIChatWorker",
    "TopicSummaryTask",
    "execute_shell",
]
