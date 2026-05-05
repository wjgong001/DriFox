# -*- coding: utf-8 -*-
"""
任务观察者数据模型
定义任务配置、执行状态、队列项等数据结构
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
import json


class TriggerMode(str, Enum):
    """触发模式"""
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    FILE_CHANGE = "file_change"


class TaskType(str, Enum):
    """任务类型"""
    ANALYZE = "analyze"
    RESEARCH = "research"
    CODE = "code"
    CUSTOM = "custom"


class OutputMode(str, Enum):
    """输出模式"""
    FILE = "file"
    CLIPBOARD = "clipboard"
    NOTIFICATION = "notification"
    WEBHOOK = "webhook"


class SessionMode(str, Enum):
    """会话模式"""
    NEW = "new"
    CONTINUE = "continue"
    FROM_SESSION_ID = "from_session_id"


class QueueStatus(str, Enum):
    """队列状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """任务状态"""
    IDLE = "idle"
    PENDING = "pending"
    PARSED = "parsed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class OutputFormat(str, Enum):
    """输出格式"""
    MARKDOWN = "markdown"
    JSON = "json"
    TEXT = "text"


@dataclass
class TriggerConfig:
    """触发配置"""
    mode: TriggerMode = TriggerMode.MANUAL  # 默认值
    # scheduled 模式
    cron: Optional[str] = None
    # manual 模式
    execute_at: Optional[str] = None
    # file_change 模式
    watch_folder: Optional[str] = None
    file_pattern: Optional[str] = "*.task.md"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TriggerConfig":
        """从字典创建触发配置"""
        mode_str = data.get("mode", "manual")
        try:
            mode = TriggerMode(mode_str)
        except ValueError:
            mode = TriggerMode.MANUAL
        
        return cls(
            mode=mode,
            cron=data.get("cron"),
            execute_at=data.get("execute_at"),
            watch_folder=data.get("watch_folder"),
            file_pattern=data.get("file_pattern", "*.task.md"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "mode": self.mode.value if isinstance(self.mode, Enum) else self.mode,
            "cron": self.cron,
            "execute_at": self.execute_at,
            "watch_folder": self.watch_folder,
            "file_pattern": self.file_pattern,
        }


@dataclass
class ContextConfig:
    """执行上下文配置"""
    session_mode: SessionMode = SessionMode.NEW
    session_id: Optional[str] = None
    agent: str = "plan"
    reference_files: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContextConfig":
        """从字典创建上下文配置"""
        session_mode_str = data.get("session_mode", "new")
        try:
            session_mode = SessionMode(session_mode_str)
        except ValueError:
            session_mode = SessionMode.NEW
        
        return cls(
            session_mode=session_mode,
            session_id=data.get("session_id"),
            agent=data.get("agent", "plan"),
            reference_files=data.get("reference_files", []),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "session_mode": self.session_mode.value if isinstance(self.session_mode, Enum) else self.session_mode,
            "session_id": self.session_id,
            "agent": self.agent,
            "reference_files": self.reference_files,
        }


@dataclass
class OutputConfig:
    """输出配置"""
    mode: OutputMode = OutputMode.FILE
    destination: Optional[str] = None
    format: OutputFormat = OutputFormat.MARKDOWN

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutputConfig":
        """从字典创建输出配置"""
        mode_str = data.get("mode", "file")
        try:
            mode = OutputMode(mode_str)
        except ValueError:
            mode = OutputMode.FILE
        
        fmt_str = data.get("format", "markdown")
        try:
            fmt = OutputFormat(fmt_str)
        except ValueError:
            fmt = OutputFormat.MARKDOWN
        
        return cls(
            mode=mode,
            destination=data.get("destination"),
            format=fmt,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "mode": self.mode.value if isinstance(self.mode, Enum) else self.mode,
            "destination": self.destination,
            "format": self.format.value if isinstance(self.format, Enum) else self.format,
        }


@dataclass
class TaskConfig:
    """任务配置"""
    id: str
    name: Optional[str] = None
    type: TaskType = TaskType.CUSTOM
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    priority: str = "normal"
    retry: int = 0
    timeout: int = 3600
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 原始内容（从任务文件解析）
    content: Optional[str] = None
    # 源文件路径
    source_file: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskConfig":
        """从字典创建任务配置"""
        # 处理 id
        task_id = data.get("id")
        if not task_id:
            import uuid
            task_id = str(uuid.uuid4())
        
        # 处理 type
        type_str = data.get("type", "custom")
        try:
            task_type = TaskType(type_str)
        except ValueError:
            task_type = TaskType.CUSTOM
        
        # 处理 trigger
        trigger_data = data.get("trigger", {})
        if isinstance(trigger_data, dict):
            trigger = TriggerConfig.from_dict(trigger_data)
        else:
            trigger = TriggerConfig(mode=TriggerMode.MANUAL)
        
        # 处理 context
        context_data = data.get("context", {})
        if isinstance(context_data, dict):
            context = ContextConfig.from_dict(context_data)
        else:
            context = ContextConfig()
        
        # 处理 output
        output_data = data.get("output", {})
        if isinstance(output_data, dict):
            output = OutputConfig.from_dict(output_data)
        else:
            output = OutputConfig()
        
        return cls(
            id=task_id,
            name=data.get("name"),
            type=task_type,
            trigger=trigger,
            context=context,
            output=output,
            priority=data.get("priority", "normal"),
            retry=data.get("retry", 0),
            timeout=data.get("timeout", 3600),
            enabled=data.get("enabled", True),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            content=data.get("content"),
            source_file=data.get("source_file"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value if isinstance(self.type, Enum) else self.type,
            "trigger": self.trigger.to_dict() if self.trigger else None,
            "context": self.context.to_dict() if self.context else None,
            "output": self.output.to_dict() if self.output else None,
            "priority": self.priority,
            "retry": self.retry,
            "timeout": self.timeout,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content": self.content,
            "source_file": self.source_file,
        }

    def to_json(self) -> str:
        """序列化为 JSON"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskConfig":
        """从 JSON 反序列化"""
        data = json.loads(json_str)
        return cls.from_dict(data)


@dataclass
class QueueItem:
    """队列项"""
    id: Optional[int] = None
    task_id: str = ""
    trigger_type: str = "manual"
    priority: int = 0
    status: QueueStatus = QueueStatus.PENDING
    execute_at: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    task_config: Optional[TaskConfig] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        """从字典创建队列项"""
        status_str = data.get("status", "pending")
        try:
            status = QueueStatus(status_str)
        except ValueError:
            status = QueueStatus.PENDING
        
        task_config_data = data.get("task_config")
        task_config = None
        if task_config_data and isinstance(task_config_data, dict):
            task_config = TaskConfig.from_dict(task_config_data)
        
        return cls(
            id=data.get("id"),
            task_id=data.get("task_id", ""),
            trigger_type=data.get("trigger_type", "manual"),
            priority=data.get("priority", 0),
            status=status,
            execute_at=data.get("execute_at"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            task_config=task_config,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "id": self.id,
            "task_id": self.task_id,
            "trigger_type": self.trigger_type,
            "priority": self.priority,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "execute_at": self.execute_at,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "retry_count": self.retry_count,
        }
        if self.task_config:
            result["task_config"] = self.task_config.to_dict()
        return result


@dataclass
class ExecutionLog:
    """执行日志"""
    id: Optional[int] = None
    task_id: str = ""
    session_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: str = "pending"
    result_summary: Optional[str] = None
    output_file: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionLog":
        """从字典创建执行日志"""
        return cls(
            id=data.get("id"),
            task_id=data.get("task_id", ""),
            session_id=data.get("session_id"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            status=data.get("status", "pending"),
            result_summary=data.get("result_summary"),
            output_file=data.get("output_file"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "result_summary": self.result_summary,
            "output_file": self.output_file,
        }


@dataclass
class TaskResult:
    """任务执行结果"""
    success: bool
    task_id: str
    session_id: Optional[str] = None
    output_content: Optional[str] = None
    output_file: Optional[str] = None
    error: Optional[str] = None
    execution_time: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "output_content": self.output_content,
            "output_file": self.output_file,
            "error": self.error,
            "execution_time": self.execution_time,
        }