# -*- coding: utf-8 -*-
"""
TaskWatcher 自动任务触发系统

纯文件化架构：
- 任务配置: *.task.md 文件
- 执行结果: 任务名_时间_uuid/ 文件夹
- 不使用数据库

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

# 文件管理（核心组件）
from .file_manager import TaskFileManager, TaskFileInfo, ExecutionResult, TASKS_FOLDER, RESULTS_FOLDER, CONFIG_FOLDER

# 执行器
from .executor import TaskExecutor

# 系统
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
    # 文件管理
    "TaskFileManager",
    "TaskFileInfo",
    "ExecutionResult",
    "TASKS_FOLDER",
    "RESULTS_FOLDER",
    "CONFIG_FOLDER",
    # 执行器
    "TaskExecutor",
    # 系统
    "TaskWatcherSystem",
]
__version__ = "2.0.0"
