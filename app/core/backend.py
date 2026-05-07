# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用
无 PyQt 依赖
"""

from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from loguru import logger

from app.core.agent import AgentManager
from app.core.chat_engine import ChatEngine
from app.core.chat_session import SessionManager, ChatSession
from app.core.memory_manager import MemoryManagerCore
from app.core.tool_executor import ToolExecutor


@dataclass
class BackendCallbacks:
    """
    后端回调函数集合 - 替代 PyQt pyqtSignal
    用于前端订阅后端事件
    """
    on_stream_started: Optional[Callable[[], None]] = None
    on_stream_chunk: Optional[Callable[[str], None]] = None
    on_stream_finished: Optional[Callable[[dict], None]] = None
    on_reasoning_content: Optional[Callable[[str], None]] = None
    on_tool_call_started: Optional[Callable[[str, str, dict], None]] = None
    on_tool_result_received: Optional[Callable[[str, str, dict, bool], None]] = None
    on_permission_requested: Optional[Callable[[str, str, dict], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_context_updated: Optional[Callable[[int, int], None]] = None


class ChatBackend:
    """
    聊天后端 - 自己创建所有核心组件，暴露统一接口给前端
    
    职责：
    1. 创建并管理 ChatEngine, SessionManager, ToolExecutor 等
    2. 暴露统一的 API 给前端（UI 层）
    3. 通过回调函数通知前端状态变化
    """
    
    def __init__(self):
        # 核心组件（后端自己创建）
        self._session_manager: Optional[SessionManager] = None
        self._chat_engine: Optional[ChatEngine] = None
        self._tool_executor: Optional[ToolExecutor] = None
        self._agent_manager: Optional[AgentManager] = None
        self._memory_manager: Optional[MemoryManagerCore] = None
        self._sub_agent_manager = None
        
        # 配置回调
        self._get_model_config: Optional[Callable] = None
        
        # 前端回调（替代 pyqtSignal）
        self._callbacks: BackendCallbacks = BackendCallbacks()
        
        # 状态
        self._initialized = False
    
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
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    # ========== 回调设置 ==========
    
    def set_callbacks(self, callbacks: BackendCallbacks):
        """设置前端回调函数"""
        self._callbacks = callbacks
    
    def _emit_stream_started(self):
        """触发流开始回调"""
        if self._callbacks.on_stream_started:
            self._callbacks.on_stream_started()
    
    def _emit_stream_chunk(self, content: str):
        """触发流片段回调"""
        if self._callbacks.on_stream_chunk:
            self._callbacks.on_stream_chunk(content)
    
    def _emit_stream_finished(self, message: dict):
        """触发流结束回调"""
        if self._callbacks.on_stream_finished:
            self._callbacks.on_stream_finished(message)
    
    def _emit_reasoning_content(self, content: str):
        """触发推理内容回调"""
        if self._callbacks.on_reasoning_content:
            self._callbacks.on_reasoning_content(content)
    
    def _emit_tool_call_started(self, tool_call_id: str, tool_name: str, arguments: dict):
        """触发工具调用开始回调"""
        if self._callbacks.on_tool_call_started:
            self._callbacks.on_tool_call_started(tool_call_id, tool_name, arguments)
    
    def _emit_tool_result_received(self, tool_call_id: str, tool_name: str, result: dict, success: bool):
        """触发工具结果回调"""
        if self._callbacks.on_tool_result_received:
            self._callbacks.on_tool_result_received(tool_call_id, tool_name, result, success)
    
    def _emit_permission_requested(self, tool_call_id: str, tool_name: str, arguments: dict):
        """触发权限请求回调"""
        if self._callbacks.on_permission_requested:
            self._callbacks.on_permission_requested(tool_call_id, tool_name, arguments)
    
    def _emit_error(self, error: str):
        """触发错误回调"""
        if self._callbacks.on_error:
            self._callbacks.on_error(error)
    
    def _emit_context_updated(self, token_count: int, limit: int):
        """触发上下文更新回调"""
        if self._callbacks.on_context_updated:
            self._callbacks.on_context_updated(token_count, limit)
    
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
        
        # 1. 创建 SessionManager
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
        
        # 4. 创建 ToolExecutor（不传递 homepage，解耦 Qt）
        self._tool_executor = ToolExecutor(workdir=workdir)
        self._tool_executor.set_memory_manager(self._memory_manager)
        self._tool_executor.set_llm_config_getter(get_model_config)
        self._tool_executor.set_agent_manager(self._agent_manager)
        logger.info("[ChatBackend] ToolExecutor 创建完成")
        
        # 5. 创建 ChatEngine
        self._chat_engine = ChatEngine(
            session_manager=self._session_manager,
            get_model_config=get_model_config,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
        )
        logger.info("[ChatBackend] ChatEngine 创建完成")
        
        self._initialized = True
        logger.info("[ChatBackend] 初始化完成")
    
    def set_callback(self, name: str, callback: Callable):
        """设置回调（代理到 ChatEngine）"""
        if self._chat_engine:
            self._chat_engine.set_callback(name, callback)
    
    def set_all_callbacks(self, callbacks: Dict[str, Callable]):
        """批量设置回调"""
        if self._chat_engine:
            for name, callback in callbacks.items():
                self._chat_engine.set_callback(name, callback)
    
    # ========== ChatEngine 代理方法 ==========
    
    def cleanup_worker(self):
        """清理 worker"""
        if self._chat_engine:
            self._chat_engine.cleanup_worker()
    
    def get_context_usage_snapshot(self, session, llm_config) -> Dict:
        """获取上下文使用快照"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage_snapshot(session, llm_config)
        return {}
    
    def switch_agent(self, agent_name: str):
        """切换 Agent"""
        if self._chat_engine:
            self._chat_engine.switch_agent(agent_name)
    
    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具调用权限"""
        if self._chat_engine:
            self._chat_engine.approve_tool_permission(tool_call_id, auto_allow, session_allow)
    
    def deny_tool_permission(self, tool_call_id: str):
        """拒绝工具调用权限"""
        if self._chat_engine:
            self._chat_engine.deny_tool_permission(tool_call_id)
    
    def provide_question_answer(self, answer: str):
        """提供问题答案"""
        if self._chat_engine:
            self._chat_engine.provide_question_answer(answer)
    
    def send_message_to_engine(self, text: str, context_params: Dict = None) -> bool:
        """发送消息到引擎"""
        if self._chat_engine:
            return self._chat_engine.send_message(text, context_params or {})
        return False
    
    # ========== ToolExecutor 代理方法 ==========
    
    def set_session_context(self, session_id: str):
        """设置会话上下文"""
        if self._tool_executor:
            self._tool_executor.set_session_context(session_id)
    
    def set_sub_agent_manager(self, manager):
        """设置子智能体管理器"""
        self._sub_agent_manager = manager
        if self._tool_executor:
            self._tool_executor.set_sub_agent_manager(manager)
    
    def reset_session_state(self):
        """重置会话状态"""
        if self._tool_executor:
            self._tool_executor.reset_session_state()
    
    def clear_todo_list(self):
        """清空待办列表"""
        if self._tool_executor:
            self._tool_executor.clear_todo_list()
    
    @property
    def todo_list(self):
        """获取待办列表"""
        if self._tool_executor:
            return self._tool_executor.todo_list
        return []
    
    @property
    def file_recorder(self):
        """获取文件操作记录器"""
        if self._tool_executor:
            return getattr(self._tool_executor, 'file_recorder', None)
        return None
    
    def execute_skill(self, method: str, params: Dict):
        """执行技能"""
        if self._tool_executor:
            return self._tool_executor.execute_skill(method, params)
        return None
    
    # ========== AgentManager 代理方法 ==========
    
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
    
    # ========== 会话管理 ==========
    
    def create_session(self) -> ChatSession:
        """创建新会话"""
        session = self._session_manager.create_new_session()
        return session
    
    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前会话"""
        return self._session_manager.get_current_session()
    
    def switch_session(self, index: int):
        """切换会话"""
        self._session_manager.switch_to_session(index)
    
    def set_current_session(self, session: ChatSession):
        """设置当前会话"""
        self._session_manager.set_current_session(session)
    
    def delete_session(self, index: int) -> bool:
        """删除会话"""
        return self._session_manager.delete_session(index)
    
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
        if self._chat_engine and self._chat_engine._current_worker:
            self._chat_engine._current_worker.stop()
    
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
