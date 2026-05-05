# -*- coding: utf-8 -*-
"""
任务执行引擎 - 使用独立环境执行任务

参考 IsolatedChatContext 的实现，为任务执行创建完全独立的环境：
1. 独立的 SessionManager（保存在指定 project）
2. 独立的 ToolExecutor
3. 不触发工具卡片 UI
4. 显示绿色任务卡片

这样任务执行与 UI 完全隔离，不会影响用户手动对话。
"""

import uuid
import time
import threading
from typing import Optional, Dict, List, Callable, Any
from datetime import datetime
from loguru import logger

from app.core.chat_engine import ChatEngine


class TaskExecutionEngine:
    """任务执行引擎 - 使用完全隔离的环境执行任务
    
    特点：
    1. 不复用现有对话窗的 ChatEngine
    2. 创建独立的隔离上下文
    3. 会话保存到任务指定的 project
    4. 不触发工具卡片 UI
    5. 支持任务状态回调
    """
    
    # 单例
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self, main_widget=None):
        """初始化任务执行引擎
        
        Args:
            main_widget: UI 主窗口（用于获取可复用的基础组件）
        """
        self._main_widget = main_widget
        self._active_tasks: Dict[str, "TaskContext"] = {}  # task_id -> TaskContext
        self._task_callbacks: Dict[str, Callable] = {}  # event -> callbacks
        
    @classmethod
    def get_instance(cls, main_widget=None) -> "TaskExecutionEngine":
        """获取单例实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(main_widget)
        return cls._instance
    
    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        if cls._instance:
            cls._instance.cleanup()
            cls._instance = None
    
    def set_callback(self, event: str, callback: Callable) -> None:
        """设置回调
        
        Args:
            event: 事件名 (task_started/task_progress/task_completed/task_failed)
            callback: 回调函数
        """
        if event not in self._task_callbacks:
            self._task_callbacks[event] = []
        self._task_callbacks[event].append(callback)
    
    def _emit(self, event: str, *args) -> None:
        """触发回调"""
        callbacks = self._task_callbacks.get(event, [])
        for cb in callbacks:
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"[TaskExecutionEngine] 回调错误 {event}: {e}")
    
    def execute_task(
        self,
        task_id: str,
        task_name: str,
        task_content: str,
        project: str = None,
        agent: str = "plan",
        callback: Optional[Callable[[str, bool], None]] = None
    ) -> str:
        """执行任务
        
        Args:
            task_id: 任务 ID
            task_name: 任务名称
            task_content: 任务内容
            project: 指定 project（会话将保存到此 project）
            agent: 使用的智能体
            callback: 完成回调 (result, success)
        
        Returns:
            session_id
        """
        # 创建隔离上下文
        isolated_ctx = self._create_isolated_context(project)
        
        # 创建任务上下文
        task_ctx = TaskContext(
            task_id=task_id,
            task_name=task_name,
            engine=isolated_ctx.engine,
            session_manager=isolated_ctx._session_manager,
            project=project or "任务执行",
            callback=callback,
        )
        
        self._active_tasks[task_id] = task_ctx
        
        # 注册引擎回调
        isolated_ctx.engine.set_callback("content_received", task_ctx.on_content_received)
        isolated_ctx.engine.set_callback("stream_finished", task_ctx.on_stream_finished)
        isolated_ctx.engine.set_callback("error", task_ctx.on_error)
        
        # 发送任务消息
        try:
            session = isolated_ctx._session_manager.get_current_session()
            session_id = session.session_id
            
            # 注入任务内容（使用 ChatSession 的方法）
            if hasattr(session, 'add_user_message'):
                session.add_user_message(f"【任务】{task_name}\n\n{task_content}")
            else:
                session.messages.append({
                    "role": "user",
                    "content": f"【任务】{task_name}\n\n{task_content}",
                    "timestamp": datetime.now().isoformat(),
                })
            
            # 触发开始回调
            self._emit("task_started", task_id, task_name, project)
            logger.info(f"[TaskExecutionEngine] 任务开始: {task_name}, session={session_id}")
            
            # 启动引擎
            success = isolated_ctx.engine.send_message(
                user_text=task_content,
                agent=agent,
            )
            
            if not success:
                raise Exception("ChatEngine.send_message 失败")
            
            return session_id
            
        except Exception as e:
            logger.error(f"[TaskExecutionEngine] 执行任务失败: {e}")
            self._emit("task_failed", task_id, str(e))
            if callback:
                callback(f"执行失败: {e}", False)
            return None
    
    def _create_isolated_context(self, project: str = None) -> "TaskExecutionContext":
        """创建隔离的执行上下文"""
        return TaskExecutionContext(
            main_widget=self._main_widget,
            project=project,
        )
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        ctx = self._active_tasks.get(task_id)
        if ctx:
            return ctx.get_status()
        return None
    
    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        ctx = self._active_tasks.get(task_id)
        if ctx:
            ctx.cancel()
            self._emit("task_cancelled", task_id)
            return True
        return False
    
    def cleanup(self) -> None:
        """清理所有任务"""
        for task_id in list(self._active_tasks.keys()):
            ctx = self._active_tasks[task_id]
            ctx.cleanup()
        self._active_tasks.clear()


class TaskContext:
    """任务执行上下文"""
    
    def __init__(
        self,
        task_id: str,
        task_name: str,
        engine: Any,
        session_manager: Any,
        project: str,
        callback: Optional[Callable] = None
    ):
        self.task_id = task_id
        self.task_name = task_name
        self.engine = engine
        self.session_manager = session_manager
        self.project = project
        self.callback = callback
        
        self._content_parts: List[str] = []
        self._start_time = time.time()
        self._is_completed = False
        self._error = None
        self._session_id = None
    
    def on_content_received(self, content: str) -> None:
        """内容接收回调"""
        self._content_parts.append(content)
    
    def on_stream_finished(self, session_id: str) -> None:
        """流结束回调"""
        self._is_completed = True
        self._session_id = session_id
        
        # 构建完整结果
        full_content = "".join(self._content_parts)
        
        # 保存会话到指定 project
        self._save_session(project=self.project)
        
        # 调用回调
        if self.callback:
            self.callback(full_content, True)
        
        # 清理引擎
        self.cleanup()
        
        logger.info(f"[TaskContext] 任务完成: {self.task_name}, 内容长度: {len(full_content)}")
    
    def on_error(self, error: str) -> None:
        """错误回调"""
        self._error = error
        self._is_completed = True
        
        if self.callback:
            self.callback(f"错误: {error}", False)
        
        self.cleanup()
        
        logger.error(f"[TaskContext] 任务失败: {self.task_name}, error={error}")
    
    def _save_session(self, project: str) -> None:
        """保存会话到指定 project"""
        try:
            session = self.session_manager.get_current_session()
            if not session:
                return
            
            # 获取消息
            messages = []
            for msg in session.messages:
                if hasattr(msg, 'role'):
                    messages.append({
                        "role": msg.role,
                        "content": msg.content,
                        "timestamp": getattr(msg, 'timestamp', ''),
                    })
            
            if not messages:
                return
            
            # 保存到 UI 的 history_manager（在指定 project）
            if self.engine._main_widget:
                history_manager = getattr(self.engine._main_widget, 'history_manager', None)
                if history_manager:
                    history_manager.save_session(
                        messages=messages,
                        title=f"【任务】{self.task_name}",
                        session_id=session.session_id,
                        project=project,
                    )
                    logger.debug(f"[TaskContext] 会话已保存: {session.session_id}, project={project}")
            
        except Exception as e:
            logger.warning(f"[TaskContext] 保存会话失败: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """获取任务状态"""
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "project": self.project,
            "is_completed": self._is_completed,
            "error": self._error,
            "session_id": self._session_id,
            "elapsed_seconds": int(time.time() - self._start_time),
            "content_length": sum(len(p) for p in self._content_parts),
        }
    
    def cancel(self) -> None:
        """取消任务"""
        if not self._is_completed:
            try:
                self.engine.stop_stream()
            except Exception:
                pass
            self._is_completed = True
            self._error = "cancelled"
    
    def cleanup(self) -> None:
        """清理资源"""
        try:
            # 停止引擎
            if hasattr(self.engine, 'stop_stream'):
                try:
                    self.engine.stop_stream()
                except Exception:
                    pass
            
            # 清理工具执行器状态
            if hasattr(self.engine, '_tool_executor') and self.engine._tool_executor:
                te = self.engine._tool_executor
                te._session_id = None
                te._call_id = None
                if hasattr(te, '_builtin_tools') and te._builtin_tools:
                    te._builtin_tools.todo_clear()
                    
        except Exception as e:
            logger.warning(f"[TaskContext] 清理失败: {e}")


class TaskExecutionContext:
    """任务执行隔离上下文"""
    
    def __init__(
        self,
        main_widget,
        project: str = None,
    ):
        self._main_widget = main_widget
        self._project = project
        
        # 创建独立的 SessionManager
        self._session_manager = self._create_session_manager()
        
        # 创建独立的 ToolExecutor
        self._tool_executor = self._create_tool_executor()
        
        # 创建 ChatEngine
        self.engine = self._create_chat_engine()
    
    def _create_session_manager(self) -> Any:
        """创建独立的 SessionManager"""
        from app.utils.chat_session import SessionManager
        
        manager = SessionManager()
        manager.create_new_session()
        
        # project 在保存会话时指定，不需要在这里设置
        # manager._project = self._project
        
        return manager
    
    def _create_tool_executor(self) -> Any:
        """创建独立的 ToolExecutor"""
        from app.core.tool_executor import ToolExecutor
        
        # 获取 UI 的 ToolExecutor
        ui_tool_executor = getattr(self._main_widget, '_tool_executor', None)
        
        # 创建隔离的 ToolExecutor
        homepage = getattr(self._main_widget, 'homepage', None)
        executor = ToolExecutor(homepage=homepage)
        
        # 复用 UI 的 BuiltinTools（只读共享）
        if ui_tool_executor and ui_tool_executor._builtin_tools:
            executor._builtin_tools = ui_tool_executor._builtin_tools
            if ui_tool_executor._canvas_tools_executor:
                executor._canvas_tools_executor = ui_tool_executor._canvas_tools_executor
        
        # 设置会话上下文
        session_id = str(uuid.uuid4())
        executor.set_session_context(session_id, call_id=None)
        executor.set_session_messages_getter(self._get_session_messages)
        
        return executor
    
    def _get_session_messages(self) -> List[Dict[str, Any]]:
        """获取会话消息"""
        session = self._session_manager.get_current_session()
        if not session:
            return []
        return list(session.messages or [])
    
    def _create_chat_engine(self) -> "TaskChatEngine":
        """创建任务执行专用的 ChatEngine"""
        # 创建自定义引擎，禁用工具卡片
        return TaskChatEngine(
            main_widget=self._main_widget,
            session_manager=self._session_manager,
            tool_executor=self._tool_executor,
        )


class TaskChatEngine:
    """任务执行专用 ChatEngine
    
    与普通 ChatEngine 的区别：
    1. 不触发工具卡片 UI
    2. 不触发消息卡片（静默执行）
    3. 自动保存会话
    """
    
    def __init__(
        self,
        main_widget,
        session_manager,
        tool_executor,
    ):
        self._main_widget = main_widget
        self._session_manager = session_manager
        self._tool_executor = tool_executor
        self._current_stream = None
        
        # 回调
        self._callbacks: Dict[str, Callable] = {}
        
        # 注入 main_widget 引用（用于获取配置）
        self._main_widget = main_widget
        
        # 保存实际引擎引用
        self._actual_engine = None
    
    def set_callback(self, event: str, callback: Callable) -> None:
        """设置回调"""
        self._callbacks[event] = callback
    
    def _emit(self, event: str, *args) -> None:
        """触发回调"""
        cb = self._callbacks.get(event)
        if cb:
            cb(*args)
    
    def _on_stream_finished(self) -> None:
        """流结束回调"""
        session = self._session_manager.get_current_session()
        session_id = session.session_id if session else None
        self._emit("stream_finished", session_id)
    
    def send_message(
        self,
        user_text: str,
        agent: str = "plan",
        stream: bool = True,
    ) -> bool:
        """发送消息"""
        from app.core.chat_engine import ChatEngine
        from app.core.agent import AgentManager
        
        # 获取模型配置
        model_config = {}
        if self._main_widget and hasattr(self._main_widget, '_get_current_model_config'):
            model_config = self._main_widget._get_current_model_config()
        
        # 如果没有配置，尝试从 Settings 获取
        if not model_config:
            try:
                from app.utils.config import Settings
                setting = Settings.get_instance()
                saved_providers = setting.llm_saved_providers.value or {}
                if saved_providers:
                    first_provider = list(saved_providers.keys())[0]
                    model_config = saved_providers[first_provider]
            except Exception:
                pass
        
        # 如果还是没有，使用默认配置
        if not model_config:
            from loguru import logger
            logger.warning("[TaskChatEngine] 未找到 LLM 配置，使用默认配置")
            model_config = {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-dummy",
                "base_url": "https://api.openai.com/v1",
            }
        
        # 获取 agent_manager
        agent_manager = None
        if self._main_widget:
            agent_manager = getattr(self._main_widget, '_agent_manager', None)
        
        # 如果没有 agent_manager，创建一个新的
        if agent_manager is None:
            agent_manager = AgentManager()
            logger.info("[TaskChatEngine] 创建新的 AgentManager")
        
        # 创建 ChatEngine
        engine = ChatEngine(
            session_manager=self._session_manager,
            get_model_config=lambda: model_config,
            get_context_provider=lambda: None,
            tool_executor=self._tool_executor,
            agent_manager=agent_manager,
            get_chat_cards=None,  # 不触发 UI
            get_memory_context=getattr(self._main_widget, '_build_memory_context_for_engine', None) if self._main_widget else None,
            worker_callbacks={
                "content_received": lambda c: self._emit("content_received", c),
                "stream_finished": lambda r: self._on_stream_finished(),
                "error": lambda e: self._emit("error", e),
            },
            api_mode=True,  # API 模式，直接回调
        )
        
        # 保存引用以便清理
        self._actual_engine = engine
        
        # 设置 agent
        if agent:
            engine.switch_agent(agent)
        
        # 发送消息
        return engine.send_message(
            user_text=user_text,
            context_params={},
        )
    
    def _on_stream_finished(self) -> None:
        """流结束回调"""
        session = self._session_manager.get_current_session()
        session_id = session.session_id if session else None
        self._emit("stream_finished", session_id)
    
    def stop_stream(self) -> None:
        """停止流"""
        if self._current_stream:
            try:
                self._current_stream.cancel()
            except Exception:
                pass
        if hasattr(self, '_actual_engine') and self._actual_engine:
            try:
                self._actual_engine._worker.cancel()
            except Exception:
                pass


# 全局实例获取函数
def get_task_execution_engine(main_widget=None) -> TaskExecutionEngine:
    """获取任务执行引擎单例"""
    return TaskExecutionEngine.get_instance(main_widget)