# -*- coding: utf-8 -*-
"""
Workers 模块 - 包含各种执行器和任务类
"""

# 新版异步 Workers（使用 threading）
from app.core.workers.async_chat_worker import AsyncOpenAIChatWorker
from app.core.workers.async_topic_summary import TopicSummaryTask
from app.core.workers.async_shell_task import execute_shell
from app.core.workers.async_subagent_worker import AsyncSubAgentExecutor

# 子智能体管理器（保留但仅导出 SubAgentManager）
from app.core.workers.subagent_worker import SubAgentManager

__all__ = [
    # 异步版本（推荐使用）
    "AsyncOpenAIChatWorker",
    "AsyncSubAgentExecutor",
    "TopicSummaryTask",
    "execute_shell",
    # 子智能体管理器
    "SubAgentManager",
]