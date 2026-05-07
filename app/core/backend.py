# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用

支持前后端分离，可接入任何形式的前端（桌面/Web/移动端）
"""

import threading
from typing import Dict, List, Any, Optional, Callable

from loguru import logger

from app.core.agent import AgentManager
from app.core.chat_engine import ChatEngine
from app.core.chat_session import SessionManager, ChatSession
from app.core.memory_manager import MemoryManagerCore
from app.core.tool_executor import ToolExecutor
from app.core.event_bus import (
    Signal,
    ChatEvents,
    get_event_bus,
    EventBus,
)
from app.core.workers import WorkerPool


class ChatBackend:
    """
    聊天后端 - 自己创建所有核心组件，暴露统一接口给前端
    
    职责：
    1. 创建并管理 ChatEngine, SessionManager, ToolExecutor 等
    2. 暴露统一的 API 给前端（UI 层）
    3. 通过 EventBus/Signal 发出状态变化信号供前端订阅
    
    前端可以通过以下方式订阅事件：
    ```python
    # 方式1: 使用 EventBus
    from app.core import get_event_bus
    bus = get_event_bus()
    bus.subscribe("stream:chunk", on_chunk)
    
    # 方式2: 使用 Signal
    backend.stream_chunk.connect(on_chunk)
    ```
    """
    
    def __init__(self, event_bus: Optional[EventBus] = None):
        """
        初始化后端
        
        Args:
            event_bus: 事件总线实例（可选，默认使用全局实例）
        """
        # 事件总线
        self._event_bus = event_bus or get_event_bus()
        
        # 核心组件（后端自己创建）
        self._session_manager: Optional[SessionManager] = None
        self._chat_engine: Optional[ChatEngine] = None
        self._tool_executor: Optional[ToolExecutor] = None
        self._agent_manager: Optional[AgentManager] = None
        self._memory_manager: Optional[MemoryManagerCore] = None
        self._sub_agent_manager = None
        
        # 配置回调
        self._get_model_config: Optional[Callable] = None
        
        # 线程池
        self._thread_pool = WorkerPool(max_workers=4)
        
        # 状态
        self._initialized = False
        
        # ========== Signal 定义（兼容 PyQt 风格的 API）==========
        # 会话相关
        self.session_created = Signal(str)  # session_id
        self.session_changed = Signal(str)  # session_id
        self.session_deleted = Signal(int)  # index
        
        # 消息相关
        self.message_received = Signal(dict)  # 新消息
        self.stream_started = Signal()  # 流式开始
        self.stream_chunk = Signal(str)  # 流式内容片段
        self.stream_finished = Signal(dict)  # 完成时的消息
        self.reasoning_content = Signal(str)  # DeepSeek thinking mode
        
        # 工具相关
        self.tool_call_started = Signal(str, str, dict)  # tool_call_id, tool_name, arguments
        self.tool_result_received = Signal(str, str, dict, bool)  # tool_call_id, name, result, success
        
        # 权限相关
        self.permission_requested = Signal(str, str, dict)  # tool_call_id, tool_name, arguments
        
        # 错误
        self.error_occurred = Signal(str)
        
        # 上下文
        self.context_updated = Signal(int, int)  # token_count, limit
        
        # 注册 Signal 到 EventBus（双向同步）
        self._register_signals_to_event_bus()
        
        logger.debug("[ChatBackend] 实例创建完成")
    
    def _register_signals_to_event_bus(self):
        """将 Signal 连接到 EventBus"""
        # 会话信号 -> EventBus
        self.session_created.connect(
            lambda sid: self._event_bus.emit(ChatEvents.SESSION_CREATED, sid)
        )
        self.session_changed.connect(
            lambda sid: self._event_bus.emit(ChatEvents.SESSION_CHANGED, sid)
        )
        self.session_deleted.connect(
            lambda idx: self._event_bus.emit(ChatEvents.SESSION_DELETED, idx)
        )
        
        # 消息信号 -> EventBus
        self.message_received.connect(
            lambda msg: self._event_bus.emit(ChatEvents.MESSAGE_RECEIVED, msg)
        )
        self.stream_started.connect(
            lambda: self._event_bus.emit(ChatEvents.STREAM_STARTED)
        )
        self.stream_chunk.connect(
            lambda chunk: self._event_bus.emit(ChatEvents.STREAM_CHUNK, chunk)
        )
        self.stream_finished.connect(
            lambda msg: self._event_bus.emit(ChatEvents.STREAM_FINISHED, msg)
        )
        self.reasoning_content.connect(
            lambda content: self._event_bus.emit(ChatEvents.REASONING_CONTENT, content)
        )
        
        # 工具信号 -> EventBus
        self.tool_call_started.connect(
            lambda tid, name, args: self._event_bus.emit(ChatEvents.TOOL_CALL_STARTED, tid, name, args)
        )
        self.tool_result_received.connect(
            lambda tid, name, result, success: self._event_bus.emit(ChatEvents.TOOL_RESULT_RECEIVED, tid, name, result, success)
        )
        
        # 权限信号 -> EventBus
        self.permission_requested.connect(
            lambda tid, name, args: self._event_bus.emit(ChatEvents.PERMISSION_REQUESTED, tid, name, args)
        )
        
        # 错误信号 -> EventBus
        self.error_occurred.connect(
            lambda err: self._event_bus.emit(ChatEvents.ERROR_OCCURRED, err)
        )
        
        # 上下文信号 -> EventBus
        self.context_updated.connect(
            lambda count, limit: self._event_bus.emit(ChatEvents.CONTEXT_UPDATED, count, limit)
        )
    
    # ========== 属性访问 ==========
    
    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager
    
    @property
    def chat_engine(self) -> ChatEngine:
        return self._chat_engine
    
    @property
    def tool_executor(self) -> ToolExecutor:
        return self._tool_executor
    
    @property
    def agent_manager(self) -> AgentManager:
        return self._agent_manager
    
    @property
    def memory_manager(self) -> MemoryManagerCore:
        return self._memory_manager
    
    @property
    def sub_agent_manager(self):
        return self._sub_agent_manager
    
    def set_sub_agent_manager(self, manager):
        """设置子智能体管理器"""
        self._sub_agent_manager = manager
    
    def set_session_context(self, session_id: str, call_id: str = None):
        """设置会话上下文（代理到 ToolExecutor）"""
        if self._tool_executor:
            self._tool_executor.set_session_context(session_id, call_id)
    
    def get_context_usage_snapshot(self, session, llm_config) -> Dict:
        """获取上下文使用快照（代理到 ChatEngine）"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage_snapshot(session, llm_config)
        return {}
    
    def send_message_to_engine(self, text: str, context_params: Dict = None) -> bool:
        """发送消息到引擎（代理到 ChatEngine）"""
        if self._chat_engine:
            return self._chat_engine.send_message(text, context_params)
        return False
    
    @property
    def file_recorder(self):
        """获取文件操作记录器（代理到 ToolExecutor）"""
        if self._tool_executor:
            return self._tool_executor.file_recorder
        return None
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    @property
    def event_bus(self) -> EventBus:
        """获取事件总线"""
        return self._event_bus
    
    # ========== 初始化 ==========
    
    def initialize(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        agent_manager: AgentManager = None,
        workdir: str = None,
        canvas_name: str = None,
    ):
        """
        后端初始化 - 自己创建所有组件（不依赖 Qt）
        
        Args:
            get_model_config: 获取模型配置的回调
            agent_manager: 已有的 AgentManager（可选）
            workdir: 工作目录
            canvas_name: 画布名称
        """
        logger.info("[ChatBackend] 初始化中...")
        
        self._get_model_config = get_model_config
        
        # 1. 创建 SessionManager（已移除 QObject 依赖）
        self._session_manager = SessionManager()
        self._session_manager.create_new_session()
        logger.info("[ChatBackend] SessionManager 创建完成")
        
        # 2. 创建 MemoryManager
        self._memory_manager = MemoryManagerCore(canvas_name or "default")
        logger.info("[ChatBackend] MemoryManager 创建完成")
        
        # 3. 使用传入的 AgentManager 或创建新的
        if agent_manager is not None:
            self._agent_manager = agent_manager
        else:
            self._agent_manager = AgentManager()
        logger.info(f"[ChatBackend] AgentManager 就绪，{len(self._agent_manager.list_agents())} 个 Agent")
        
        # 4. 创建 ToolExecutor
        self._tool_executor = ToolExecutor(workdir=workdir)
        self._tool_executor.set_memory_manager(self._memory_manager)
        self._tool_executor.set_llm_config_getter(get_model_config)
        self._tool_executor.set_agent_manager(self._agent_manager)
        # 传递事件总线给 ToolExecutor
        self._tool_executor.set_event_bus(self._event_bus)
        logger.info("[ChatBackend] ToolExecutor 创建完成")
        
        # 5. 创建 ChatEngine
        self._chat_engine = ChatEngine(
            session_manager=self._session_manager,
            get_model_config=get_model_config,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
        )
        # 设置事件总线
        self._chat_engine.set_event_bus(self._event_bus)
        # 连接信号
        self._connect_chat_engine_signals()
        logger.info("[ChatBackend] ChatEngine 创建完成")
        
        # 6. 设置主线程 ID（用于跨线程通信）
        self._event_bus.set_main_thread()
        
        self._initialized = True
        logger.info("[ChatBackend] 初始化完成")
    
    def _connect_chat_engine_signals(self):
        """连接 ChatEngine 的信号"""
        if self._chat_engine is None:
            return
        
        # 连接 ChatEngine 的回调到后端信号
        if hasattr(self._chat_engine, 'on_stream_chunk'):
            # ChatEngine 使用回调模式，这里不需要额外连接
            pass
        
        # 监听 EventBus 中的事件并转发到 Signal
        def on_stream_chunk(chunk: str):
            self.stream_chunk.emit(chunk)
        
        def on_stream_finished(msg: dict):
            self.stream_finished.emit(msg)
        
        def on_tool_call(tid: str, name: str, args: dict):
            self.tool_call_started.emit(tid, name, args)
        
        def on_tool_result(tid: str, name: str, result: dict, success: bool):
            self.tool_result_received.emit(tid, name, result, success)
        
        def on_error(err: str):
            self.error_occurred.emit(err)
        
        # 订阅 ChatEngine 发布的事件
        self._event_bus.subscribe(ChatEvents.STREAM_CHUNK, on_stream_chunk)
        self._event_bus.subscribe(ChatEvents.STREAM_FINISHED, on_stream_finished)
        self._event_bus.subscribe(ChatEvents.TOOL_CALL_STARTED, on_tool_call)
        self._event_bus.subscribe(ChatEvents.TOOL_RESULT_RECEIVED, on_tool_result)
        self._event_bus.subscribe(ChatEvents.ERROR_OCCURRED, on_error)
    
    def set_callback(self, name: str, callback: Callable):
        """设置回调（代理到 ChatEngine）"""
        if self._chat_engine:
            self._chat_engine.set_callback(name, callback)
    
    def set_all_callbacks(self, callbacks: Dict[str, Callable]):
        """批量设置回调"""
        if self._chat_engine:
            for name, callback in callbacks.items():
                self._chat_engine.set_callback(name, callback)
    
    def get_primary_agents(self) -> List:
        """获取主 Agent 列表"""
        if self._agent_manager:
            return self._agent_manager.list_primary_agents()
        return []
    
    def get_agent(self, name: str):
        """获取指定 Agent"""
        if self._agent_manager:
            return self._agent_manager.get_agent(name)
        return None
    
    def switch_agent(self, agent_name: str):
        """切换 Agent"""
        if self._chat_engine:
            self._chat_engine._current_agent = agent_name
            logger.info(f"[ChatBackend] 切换 Agent: {agent_name}")
    
    # ========== 会话管理 ==========
    
    def create_session(self) -> ChatSession:
        """创建新会话"""
        session = self._session_manager.create_new_session()
        self.session_created.emit(session.session_id)
        return session
    
    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前会话"""
        return self._session_manager.get_current_session()
    
    def switch_session(self, index: int):
        """切换会话"""
        self._session_manager.switch_to_session(index)
        session = self.get_current_session()
        if session:
            self.session_changed.emit(session.session_id)
    
    def set_current_session(self, session: ChatSession):
        """设置当前会话"""
        self._session_manager.set_current_session(session)
        if session:
            self.session_changed.emit(session.session_id)
    
    def delete_session(self, index: int) -> bool:
        """删除会话"""
        result = self._session_manager.delete_session(index)
        if result:
            self.session_deleted.emit(index)
        return result
    
    def get_all_sessions(self) -> List[ChatSession]:
        """获取所有会话"""
        return self._session_manager.get_all_sessions()
    
    # ========== 对话操作 ==========
    
    def send_message(self, text: str, agent_name: str = None, **kwargs):
        """发送消息"""
        session = self.get_current_session()
        if not session:
            session = self.create_session()
        
        session.add_user_message(text, params=kwargs)
        
        self._chat_engine.send_message(
            text,
            session=session,
            agent_name=agent_name,
        )
    
    def stop_streaming(self):
        """停止流式输出"""
        if self._chat_engine and hasattr(self._chat_engine, '_current_worker'):
            worker = self._chat_engine._current_worker
            if worker:
                worker.stop()
    
    def approve_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准权限"""
        if self._chat_engine:
            self._chat_engine.approve_tool_permission(tool_call_id, auto_allow, session_allow)
    
    def deny_permission(self, tool_call_id: str):
        """拒绝权限"""
        if self._chat_engine:
            self._chat_engine.deny_tool_permission(tool_call_id)
    
    # ========== 状态查询 ==========
    
    def get_current_agent(self) -> str:
        """获取当前 Agent"""
        return self._chat_engine._current_agent if self._chat_engine else "plan"
    
    def set_current_agent(self, agent_name: str):
        """设置当前 Agent"""
        if self._chat_engine:
            self._chat_engine._current_agent = agent_name
    
    def get_context_usage(self) -> tuple:
        """获取上下文使用情况"""
        if self._chat_engine:
            return self._chat_engine._get_context_usage()
        return (0, 0)
    
    # ========== 生命周期管理 ==========
    
    def shutdown(self):
        """关闭后端，清理资源"""
        logger.info("[ChatBackend] 关闭中...")
        
        # 停止所有 Worker
        self._thread_pool.stop_all()
        
        # 清理 ChatEngine
        if self._chat_engine:
            self._chat_engine.cleanup_worker()
        
        # 清空事件监听
        self._event_bus.clear()
        
        self._initialized = False
        logger.info("[ChatBackend] 关闭完成")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False
