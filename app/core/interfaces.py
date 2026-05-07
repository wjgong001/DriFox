# -*- coding: utf-8 -*-
"""
Core 层抽象接口定义

这些接口用于解耦业务逻辑和具体实现，使得 Core 层可以独立于 UI 框架运行。
支持多前端架构：PyQt Desktop / Web / Mobile 等通过适配器调用 Core。
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional, Dict, Any, List, Callable

# 从 models.py 导入基础数据类型（避免重复定义）
from app.core.models import (
    MessageRole,
    ChatMessage,
    StreamChunk,
    ToolCall,
    Session,
)


class IChatEngine(ABC):
    """对话引擎抽象接口"""

    @abstractmethod
    async def chat_stream(
        self,
        session: Session,
        message: str,
        model_config: Dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        """
        流式对话

        Args:
            session: 会话对象
            message: 用户输入消息
            model_config: 模型配置

        Yields:
            StreamChunk: 流式输出片段
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止当前流式输出"""
        pass

    @abstractmethod
    def get_context_usage(self) -> tuple[int, int]:
        """
        获取上下文使用情况

        Returns:
            tuple[used_tokens, limit_tokens]
        """
        pass


class IToolExecutor(ABC):
    """工具执行器抽象接口"""

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        context: Dict[str, Any],
    ) -> tuple[bool, Any]:
        """
        执行工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            context: 执行上下文

        Returns:
            tuple[success, result]
        """
        pass

    @abstractmethod
    def get_available_tools(self) -> List[Dict[str, Any]]:
        """
        获取可用工具列表

        Returns:
            工具定义列表，每个工具包含 name, description, parameters 等
        """
        pass

    @abstractmethod
    def register_callback(self, callback: Callable) -> None:
        """
        注册执行回调

        Args:
            callback: 回调函数，签名: callback(tool_name, arguments, result)
        """
        pass


class ISessionManager(ABC):
    """会话管理器抽象接口"""

    @abstractmethod
    def create_session(self, canvas_id: str, agent_name: str = "plan") -> Session:
        """
        创建新会话

        Args:
            canvas_id: 画布 ID
            agent_name: Agent 名称

        Returns:
            新创建的 Session 对象
        """
        pass

    @abstractmethod
    def get_session(self, session_id: str) -> Optional[Session]:
        """
        获取会话

        Args:
            session_id: 会话 ID

        Returns:
            Session 对象或 None
        """
        pass

    @abstractmethod
    def add_message(self, session_id: str, message: ChatMessage) -> None:
        """
        添加消息到会话

        Args:
            session_id: 会话 ID
            message: 消息对象
        """
        pass

    @abstractmethod
    def get_messages(self, session_id: str) -> List[ChatMessage]:
        """
        获取会话消息列表

        Args:
            session_id: 会话 ID

        Returns:
            消息列表
        """
        pass

    @abstractmethod
    def delete_session(self, session_id: str) -> bool:
        """
        删除会话

        Args:
            session_id: 会话 ID

        Returns:
            是否成功删除
        """
        pass

    @abstractmethod
    def list_sessions(self, canvas_id: str) -> List[Session]:
        """
        列出画布下的所有会话

        Args:
            canvas_id: 画布 ID

        Returns:
            会话列表
        """
        pass


class IAgentManager(ABC):
    """Agent 管理器抽象接口"""

    @abstractmethod
    def get_agent(self, name: str) -> Optional[Dict[str, Any]]:
        """
        获取 Agent 配置

        Args:
            name: Agent 名称

        Returns:
            Agent 配置字典或 None
        """
        pass

    @abstractmethod
    def list_agents(self) -> List[str]:
        """
        列出所有 Agent

        Returns:
            Agent 名称列表
        """
        pass

    @abstractmethod
    def get_agent_tools(self, name: str) -> List[str]:
        """
        获取 Agent 可用工具列表

        Args:
            name: Agent 名称

        Returns:
            工具名称列表
        """
        pass


class IContextProvider(ABC):
    """上下文提供者接口"""

    @abstractmethod
    def get_context(self, session_id: str) -> str:
        """
        获取上下文内容（如记忆）

        Args:
            session_id: 会话 ID

        Returns:
            上下文文本内容
        """
        pass