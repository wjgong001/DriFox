# -*- coding: utf-8 -*-
"""
LLM Chatter 核心模块
提供聊天引擎、工具执行器、记忆管理等核心功能
"""

from app.core.backend import ChatBackend, BackendCallbacks
from app.core.chat_engine import ChatEngine
from app.core.tool_executor import ToolExecutor
from app.core.memory_manager import MemoryManagerCore
from app.core.agent import Agent, AgentManager, create_agent_manager
from app.core.workers import (
    OpenAIChatWorker,
    SubAgentExecutor,
    SubAgentManager,
    TopicSummaryTask,
    ShellExecutionTask,
)
from app.core.store import SessionStore, SubAgentLogStore
from app.core.message_content import (
    consolidate_messages,
    content_to_text,
    content_to_markdown,
    group_messages_for_display,
    to_api_message,
    messages_to_api,
    append_text_block,
    ensure_content_blocks,
    make_tool_result_block,
    get_user_round_ranges,
)
from app.core.retry_helper import create_api_call_with_retry, retry_on_api_error
from app.core.token_estimator import estimate_tokens, count_messages_tokens, TokenCounter
from app.core.chat_session import ChatSession, SessionManager

# 基础数据模型（纯 Python，无外部依赖）
from app.core.models import (
    generate_id,
    now,
    now_str,
    MessageRole,
    ChatMessage,
    StreamChunk,
    ToolCall,
    Session,
    CompactionState,
    CompactionCache,
)

# Core 抽象接口
from app.core.interfaces import (
    IChatEngine,
    IToolExecutor,
    ISessionManager,
    IAgentManager,
    IContextProvider,
    ChatMessage,
    StreamChunk,
    ToolCall,
    Session,
    MessageRole,
)

__all__ = [
    # Backend
    "ChatBackend",
    "BackendCallbacks",
    # 引擎与执行器
    "ChatEngine",
    "ToolExecutor",
    "MemoryManagerCore",
    # Agent 系统
    "Agent",
    "AgentManager",
    "create_agent_manager",
    # Worker
    "OpenAIChatWorker",
    "SubAgentExecutor",
    "SubAgentManager",
    "TopicSummaryTask",
    "ShellExecutionTask",
    # Store
    "SessionStore",
    "SubAgentLogStore",
    # 消息处理
    "consolidate_messages",
    "content_to_text",
    "content_to_markdown",
    "group_messages_for_display",
    "to_api_message",
    "messages_to_api",
    "append_text_block",
    "ensure_content_blocks",
    "make_tool_result_block",
    "get_user_round_ranges",
    # 重试
    "create_api_call_with_retry",
    "retry_on_api_error",
    # Token
    "estimate_tokens",
    "count_messages_tokens",
    "TokenCounter",
    # 会话
    "ChatSession",
    "SessionManager",
    # 基础数据模型
    "generate_id",
    "now",
    "now_str",
    "CompactionState",
    "CompactionCache",
    # Core 抽象接口
    "IChatEngine",
    "IToolExecutor",
    "ISessionManager",
    "IAgentManager",
    "IContextProvider",
    "ChatMessage",
    "StreamChunk",
    "ToolCall",
    "Session",
    "MessageRole",
]
