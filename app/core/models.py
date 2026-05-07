# -*- coding: utf-8 -*-
"""
Core 层基础数据模型
提供 ID 生成、时间处理等基础工具函数
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import uuid4
from enum import Enum


def generate_id() -> str:
    """生成唯一 ID (UUID4)"""
    return str(uuid4())


def now() -> datetime:
    """获取当前时间"""
    return datetime.now()


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """获取当前时间的字符串格式"""
    return datetime.now().strftime(fmt)


class MessageRole(str, Enum):
    """消息角色枚举"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class ChatMessage:
    """聊天消息"""
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    timestamp: Optional[str] = None
    params: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = now_str()
        if self.params is None:
            self.params = {}


@dataclass
class StreamChunk:
    """流式输出片段"""
    type: str  # "content" | "tool_call" | "tool_result" | "done" | "error"
    data: Any
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: Dict[str, Any]
    result: Optional[str] = None
    success: bool = True


@dataclass
class Session:
    """会话"""
    id: str
    canvas_id: str
    agent_name: str = "plan"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    messages: List[ChatMessage] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = now()
        if self.updated_at is None:
            self.updated_at = now()
        if self.messages is None:
            self.messages = []
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "canvas_id": self.canvas_id,
            "agent_name": self.agent_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tool_call_id": m.tool_call_id,
                    "tool_name": m.tool_name,
                    "timestamp": m.timestamp,
                    "params": m.params,
                }
                for m in self.messages
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        """从字典创建"""
        messages = [
            ChatMessage(
                role=m["role"],
                content=m["content"],
                tool_call_id=m.get("tool_call_id"),
                tool_name=m.get("tool_name"),
                timestamp=m.get("timestamp"),
                params=m.get("params", {}),
            )
            for m in data.get("messages", [])
        ]
        return cls(
            id=data.get("id", generate_id()),
            canvas_id=data.get("canvas_id", ""),
            agent_name=data.get("agent_name", "plan"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
            messages=messages,
            metadata=data.get("metadata", {}),
        )


@dataclass
class CompactionState:
    """压缩状态"""
    active: bool = False
    source: str = ""
    kind: str = ""
    original_count: int = 0
    summarized_count: int = 0
    kept_count: int = 0
    summary_count: int = 0
    note: str = ""


@dataclass
class CompactionCache:
    """压缩缓存"""
    active: bool = False
    kind: str = ""
    cutoff_index: int = 0
    source_message_count: int = 0
    summarized_count: int = 0
    tail_count: int = 0
    budget_tokens: int = 0
    summary_message: Optional[Dict[str, Any]] = None
    generated_at: Optional[str] = None