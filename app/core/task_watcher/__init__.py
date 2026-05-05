# -*- coding: utf-8 -*-
"""
TaskWatcher 自动任务触发系统

文件夹监听 + 定时任务 + 自动执行的 LLM 任务编排系统
"""

from .models import (
    TriggerMode,
    TaskType,
    OutputMode,
    SessionMode,
    QueueStatus,
    TaskStatus,
    OutputFormat,
    TriggerConfig,
    ContextConfig,
    OutputConfig,
    TaskConfig,
    QueueItem,
    ExecutionLog,
    TaskResult,
)

from .parser import TaskParser, TaskParseError
from .database import Database
from .config_store import TaskConfigStore
from .queue import TaskQueue
from .executor import TaskExecutor
from .watcher import TaskWatcher, TaskFileHandler
from .scheduler import TaskScheduler
from .output_handler import OutputHandler
from .engine_scheduler import EngineScheduler, get_engine_scheduler, EngineInfo
from .system import TaskWatcherSystem

__all__ = [
    # 模型
    "TriggerMode",
    "TaskType",
    "OutputMode",
    "SessionMode",
    "QueueStatus",
    "TaskStatus",
    "OutputFormat",
    "TriggerConfig",
    "ContextConfig",
    "OutputConfig",
    "TaskConfig",
    "QueueItem",
    "ExecutionLog",
    "TaskResult",
    # 解析器
    "TaskParser",
    "TaskParseError",
    # 数据库
    "Database",
    # 组件
    "TaskConfigStore",
    "TaskQueue",
    "TaskExecutor",
    "TaskWatcher",
    "TaskFileHandler",
    "TaskScheduler",
    "OutputHandler",
    "EngineScheduler",
    "EngineInfo",
    "get_engine_scheduler",
    # 系统
    "TaskWatcherSystem",
]

__version__ = "1.0.0"