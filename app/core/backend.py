# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用
"""
import os
import orjson as json
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from loguru import logger

from app.core.store import SessionStore
from app.core.agent import AgentManager
from app.core.chat_engine import ChatEngine
from app.core.chat_session import SessionManager, ChatSession
from app.core.memory_manager import MemoryManagerCore
from app.core.hook_manager import HookManager
from app.core.tool_executor import ToolExecutor
from app.utils.history_manager import HistoryManager


class ChatBackend(QObject):
    """
    聊天后端 - 自己创建所有核心组件，暴露统一接口给前端
    
    职责：
    1. 创建并管理 ChatEngine, SessionManager, ToolExecutor 等
    2. 暴露统一的 API 给前端（UI 层）
    3. 发出状态变化信号供前端订阅
    """
    
    # ========== 信号定义 ==========
    # 会话相关
    session_created = pyqtSignal(str)  # session_id
    session_changed = pyqtSignal(str)  # session_id
    session_deleted = pyqtSignal(int)  # index
    
    # 消息相关
    message_received = pyqtSignal(dict)  # 新消息
    stream_started = pyqtSignal()
    stream_chunk = pyqtSignal(str)  # 流式内容片段
    stream_finished = pyqtSignal(dict)  # 完成时的消息
    reasoning_content = pyqtSignal(str)  # DeepSeek thinking mode
    
    # 工具相关
    tool_call_started = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments
    tool_result_received = pyqtSignal(str, str, dict, bool)  # tool_call_id, name, result, success
    
    # 权限相关
    permission_requested = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments
    
    # 错误
    error_occurred = pyqtSignal(str)
    
    # 任务相关
    task_started = pyqtSignal(str, str)  # task_id, task_name
    task_completed = pyqtSignal(str, str)  # task_id, task_name
    task_failed = pyqtSignal(str, str, str)  # task_id, task_name, error
    task_progress = pyqtSignal(str, str, str)  # task_id, task_name, message
    
    # 上下文
    context_updated = pyqtSignal(int, int)  # token_count, limit
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 核心组件（后端自己创建）
        self._session_manager: Optional[SessionManager] = None
        self._chat_engine: Optional[ChatEngine] = None
        self._tool_executor: Optional[ToolExecutor] = None
        self._agent_manager: Optional[AgentManager] = None
        self._memory_manager: Optional[MemoryManagerCore] = None
        self._hook_manager: Optional[HookManager] = None
        self._sub_agent_manager = None
        self._session_store = None
        self._history_manager = None
        self._task_watcher = None
        
        # 配置回调
        self._get_model_config: Optional[Callable] = None
        
        # 线程池
        self._thread_pool = QThreadPool()
        
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
    def hook_manager(self) -> Optional[HookManager]:
        return self._hook_manager
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def session_store(self):
        return self._session_store

    @property
    def history_manager(self):
        return self._history_manager

    @property
    def task_watcher(self):
        """获取任务观察者系统"""
        return self._task_watcher
    
    # ========== 初始化 ==========
    
    def initialize(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        workdir: str = None,
    ):
        """
        后端初始化 - 自己创建所有组件（不依赖 Qt）
        
        Args:
            get_model_config: 获取模型配置的回调
            agent_manager: 已有的 AgentManager（可选）
            workdir: 工作目录
        """
        logger.info("[ChatBackend] 初始化中...")
        
        self._get_model_config = get_model_config
        
        # 1. 创建 SessionManager
        self._session_store = SessionStore.get_instance()
        self._session_manager = SessionManager()
        logger.info("[ChatBackend] SessionManager 创建完成")
        
        # 2. 创建 MemoryManager
        self._memory_manager = MemoryManagerCore()
        logger.info("[ChatBackend] MemoryManager 创建完成")
        
        # 3. 创建 HookManager（必须在 create_session 之前）
        self._hook_manager = HookManager(self._thread_pool)
        # Hook 完成后，把输出添加到上下文
        def on_hook_finished(event_name: str, output: str, success: bool):
            logger.info(f"[HookManager] Hook callback: event={event_name}, success={success}")
            
            # 只有成功执行的 hook 才添加到消息列表
            if not success:
                return
            
            hook_output = f"<hook event=\"{event_name}\">\n{output}\n</hook>"
            
            # SessionStart 和 PreUserMessage 添加到消息列表
            add_to_messages = event_name in ("SessionStart", "PreUserMessage", "PostUserMessage")
            
            if add_to_messages:
                session = self.get_current_session()
                if session:
                    # 对于 PreUserMessage，先删除之前的同类 hook 消息，只保留最新一个
                    if event_name == "PreUserMessage":
                        session.messages = [
                            msg for msg in session.messages
                            if not (msg.get("role") == "assistant" and "<hook " in (msg.get("content") or "") and 'event="PreUserMessage"' in (msg.get("content") or ""))
                        ]
                    
                    # 添加新消息
                    session.add_assistant_message(hook_output)
                
                # 发送消息给前端显示
                self.message_received.emit({
                    "role": "assistant",
                    "content": hook_output
                })
                logger.info(f"[HookManager] Hook added to messages: {event_name}")
        self._hook_manager.set_on_finished_callback(on_hook_finished)
        
        # 4. 使用 create_session() 触发 SessionStart hook（必须在 hook_manager 之后）
        self.create_session()
        
        # 3. 使用传入的 AgentManager 或创建新的
        self._agent_manager = AgentManager(str(Path(__file__).parent.parent / "agents"), self._hook_manager)
        logger.info(f"[ChatBackend] AgentManager 就绪，{len(self._agent_manager.list_agents())} 个 Agent")
        
        # 加载 .drifox 全局 hooks
        from app.utils.utils import get_app_data_dir
        global_hooks_file = get_app_data_dir() / "hooks" / "hooks.json"
        if global_hooks_file.exists():
            try:
                with open(global_hooks_file, 'r', encoding='utf-8') as f:
                    config = json.loads(f.read())
                skill_root = str(global_hooks_file.parent)
                count = self._hook_manager.register_hooks_from_json("__global__", skill_root, config)
                if count > 0:
                    logger.info(f"[ChatBackend] Loaded {count} global hooks from {global_hooks_file}")
            except Exception as e:
                logger.error(f"[ChatBackend] Failed to load global hooks from {global_hooks_file}: {e}")
        
        # 4. 创建 ToolExecutor（不传递 homepage，解耦 Qt）
        self._tool_executor = ToolExecutor(workdir=workdir, backend=self)
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
            get_chat_cards=getattr(self, '_build_chat_cards_context', None),
            get_memory_context=getattr(self, '_build_memory_context', None),
        )
        logger.info("[ChatBackend] ChatEngine 创建完成")

        self._history_manager = HistoryManager()
        
        # 6. 初始化 TaskWatcher（跳过 ChatEngine/SessionManager）
        self._init_task_watcher(get_model_config)
        
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
    
    def stop_streaming(self):
        """停止流式输出"""
        if self._chat_engine:
            return self._chat_engine.stop()
    
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
    
    def send_message_to_engine(self, text: str) -> bool:
        """发送消息到引擎"""
        if self._chat_engine:
            return self._chat_engine.send_message(text)
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
    
    # ========== 任务系统 ==========
    
    def _init_task_watcher(self, get_model_config: Callable):
        """初始化任务观察者系统"""
        try:
            from app.core.task_watcher import TaskWatcherSystem
            self._task_watcher = TaskWatcherSystem(
                get_model_config=get_model_config,
                tool_executor=self._tool_executor,
            )
            self._task_watcher.set_callback('task_started', self._on_task_started)
            self._task_watcher.set_callback('task_completed', self._on_task_completed)
            self._task_watcher.set_callback('task_failed', self._on_task_failed)
            self._task_watcher.set_callback('task_progress', self._on_task_progress)
            self._task_watcher.start()
            logger.info("[ChatBackend] TaskWatcher 初始化完成")
        except Exception as e:
            logger.error(f"[ChatBackend] TaskWatcher 初始化失败: {e}")
    
    def _on_task_started(self, task_id: str, task_name: str):
        self.task_started.emit(task_id, task_name)
    
    def _on_task_completed(self, task_id: str, task_name: str):
        self.task_completed.emit(task_id, task_name)
    
    def _on_task_failed(self, task_id: str, task_name: str, error: str):
        self.task_failed.emit(task_id, task_name, error)
    
    def _on_task_progress(self, task_id: str, task_name: str, message: str):
        self.task_progress.emit(task_id, task_name, message)
    
    def execute_task(self, config) -> str:
        """手动执行任务"""
        if self._task_watcher:
            return self._task_watcher.execute_task(config)
        return ""
    
    def get_registered_tasks(self) -> List[Dict]:
        """获取已注册的任务列表"""
        if self._task_watcher:
            return self._task_watcher.get_registered_tasks()
        return []
    
    def get_execution_results(self, limit: int = 50) -> List[Dict]:
        """获取执行结果"""
        if self._task_watcher:
            return self._task_watcher.get_execution_results(limit)
        return []
    
    def reset_session_state(self):
        """重置会话状态"""
        if self._tool_executor:
            self._tool_executor.reset_session_state()
    
    def clear_todo_list(self):
        """清空待办列表"""
        if self._tool_executor:
            self._tool_executor.clear_todo_list()
    
    def get_todos(self):
        """获取待办列表（返回副本）"""
        if self._tool_executor:
            return self._tool_executor.get_todos()
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
    
    # ========== MemoryManager 代理方法 ==========
    
    def get_memory_context_string(self, query: str = "", limit: int = 8) -> str:
        """获取记忆上下文字符串"""
        if self._memory_manager:
            return self._memory_manager.get_context_string(query=query, limit=limit)
        return ""
    
    def get_user_memories(self, memory_data: Dict = None) -> List[Dict]:
        """获取用户记忆列表"""
        if self._memory_manager:
            return self._memory_manager.get_user_memories(memory_data=memory_data)
        return []
    
    def load_memory_data(self) -> Dict:
        """加载记忆数据"""
        if self._memory_manager:
            return self._memory_manager.load_memory()
        return {}
    
    def add_user_memory(self, content: str, **kwargs):
        """添加用户记忆"""
        if self._memory_manager:
            self._memory_manager.add_user_memory(content, **kwargs)
    
    def touch_memories(self, contents: List[str], memory_data: Dict = None) -> bool:
        """标记记忆被访问"""
        if self._memory_manager:
            return self._memory_manager.touch_memories(contents, memory_data=memory_data)
        return False
    
    def save_memory_data(self, memory_data: Dict) -> bool:
        """保存记忆数据"""
        if self._memory_manager:
            return self._memory_manager.save_memory(memory_data)
        return False
    
    def update_user_memories(self, memories: List[Dict]) -> bool:
        """更新用户记忆"""
        if self._memory_manager:
            return self._memory_manager.update_user_memories(memories)
        return False
    
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
        self.session_created.emit(session.session_id)
        
        # Trigger SessionStart hook
        if self._hook_manager:
            context = {
                "project_root": os.getcwd(),
            }
            self._hook_manager.trigger_event(
                "SessionStart",
                context=context,
                current_message=""
            )
        
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
    
    # ========== 状态查询 ==========
    
    def get_current_agent(self) -> str:
        """获取当前 Agent"""
        if self._chat_engine:
            return self._chat_engine.current_agent
        return "plan"
    
    def set_current_agent(self, agent_name: str):
        """设置当前 Agent"""
        if self._chat_engine:
            self._chat_engine.set_current_agent(agent_name)
    
    def set_streaming_state(self, is_streaming: bool):
        """设置流式状态"""
        if self._chat_engine:
            self._chat_engine.set_streaming(is_streaming)
    
    def get_context_usage(self) -> tuple:
        """获取上下文使用情况"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage()
        return (0, 0)
    
    # ========== 上下文构建方法 ==========
    
    def _build_memory_context(self, query: str = "") -> str:
        """构建长期记忆上下文（供 ChatEngine 调用）"""
        if not self._memory_manager:
            return ""
        return self._memory_manager.get_context_string(query=query, limit=8)
    
    def _build_chat_cards_context(self) -> str:
        """构建卡片上下文"""
        # 如果有 get_chat_cards 回调，调用它
        if self._get_chat_cards:
            cards = self._get_chat_cards()
            if cards:
                return "\n\n# 已启用的卡片\n" + "\n".join(cards)
        return ""
