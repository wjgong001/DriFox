# -*- coding: utf-8 -*-
"""
任务执行器
使用 EngineScheduler 分配空闲引擎执行任务
"""

import time
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List
from loguru import logger

from .models import TaskConfig, TaskResult, SessionMode
from .database import Database
from .engine_scheduler import get_engine_scheduler, EngineScheduler


class TaskExecutor:
    """任务执行器
    
    通过 EngineScheduler 分配空闲的 ChatEngine 执行任务
    """

    def __init__(
        self,
        scheduler: Optional[EngineScheduler] = None,
        event_bus: Optional[Any] = None,
        db: Optional[Database] = None
    ):
        """初始化任务执行器
        
        Args:
            scheduler: 引擎调度器（默认使用全局调度器）
            event_bus: 事件总线（可选）
            db: 数据库实例（可选）
        """
        self._scheduler = scheduler or get_engine_scheduler()
        self._event_bus = event_bus
        self._db = db or Database.get_instance()
        
        # 任务执行回调
        self._callbacks: Dict[str, Callable] = {}
        
        # 任务状态追踪
        self._current_task: Optional[TaskConfig] = None
        self._current_engine_id: Optional[str] = None
        self._current_session_id: Optional[str] = None
        self._current_project: Optional[str] = None
        self._collected_content: List[str] = []
        self._task_start_time: Optional[float] = None
        
        # 回调 ID（用于清理）
        self._engine_callback_ids: List[str] = []

    def set_callback(self, event: str, callback: Callable) -> None:
        """设置任务事件回调
        
        Args:
            event: 事件名 (task_started/task_completed/task_failed)
            callback: 回调函数
        """
        self._callbacks[event] = callback

    def _emit(self, event: str, *args) -> None:
        """触发事件"""
        callback = self._callbacks.get(event)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[TaskExecutor] 回调错误 {event}: {e}")
        
        if self._event_bus:
            self._event_bus.emit(event, *args)

    def execute(self, config: TaskConfig, queue_id: Optional[int] = None) -> None:
        """执行任务（非阻塞，事件驱动）
        
        Args:
            config: 任务配置
            queue_id: 队列项 ID（用于更新状态）
        """
        # 分配引擎
        allocation = self._scheduler.allocate_engine(config.id, config.context.session_id)
        if not allocation:
            logger.error("[TaskExecutor] 无法分配引擎")
            self._emit("task_failed", config, "没有可用的 ChatEngine")
            return
        
        engine_id, chat_engine, session_manager, session_project = allocation
        
        # 保存任务状态
        self._current_task = config
        self._current_engine_id = engine_id
        self._current_session_id = None
        self._current_project = session_project
        self._collected_content = []
        self._task_start_time = time.time()
        
        logger.info(f"[TaskExecutor] 开始执行任务: {config.id}, engine={engine_id}, project={session_project}")
        self._emit("task_started", config)
        
        try:
            # 注册引擎回调
            self._register_engine_callbacks(chat_engine)
            
            # 1. 创建会话
            session_id = self._create_session(session_manager, config, session_project)
            if not session_id:
                raise Exception("创建会话失败")
            
            self._current_session_id = session_id
            
            # 2. 注入任务内容
            self._inject_task_content(session_manager, session_id, config)
            
            # 3. 调用 ChatEngine 发送消息
            success = self._send_to_engine(chat_engine, session_manager, session_id, config)
            if not success:
                raise Exception("ChatEngine 发送消息失败")
            
            # 注意：结果将在 stream_finished 回调中处理
            
        except Exception as e:
            self._handle_error(str(e))

    def _register_engine_callbacks(self, chat_engine: Any) -> None:
        """注册 ChatEngine 的回调"""
        # 清空之前的回调
        for cb_id in self._engine_callback_ids:
            # 尝试移除之前的回调（如果 ChatEngine 支持）
            pass
        
        self._engine_callback_ids = [
            "content_received", "stream_finished", "error",
            "tool_call_started", "tool_result_received", "messages_updated"
        ]
        
        # 流式内容接收
        chat_engine.set_callback("content_received", self._on_content_received)
        chat_engine.set_callback("stream_finished", self._on_stream_finished)
        chat_engine.set_callback("error", self._on_engine_error)
        chat_engine.set_callback("tool_call_started", self._on_tool_started)
        chat_engine.set_callback("tool_result_received", self._on_tool_result)
        chat_engine.set_callback("messages_updated", self._on_messages_updated)

    def _on_content_received(self, content: str):
        """ChatEngine 内容片段回调"""
        if self._current_task:
            self._collected_content.append(content)

    def _on_stream_finished(self, full_content: str):
        """ChatEngine 流结束回调"""
        if self._current_task:
            self.on_chat_finished(full_content)

    def _on_engine_error(self, error: str):
        """ChatEngine 错误回调"""
        if self._current_task:
            self._handle_error(error)

    def _on_tool_started(self, tool_name: str, tool_id: str, arguments: dict, agent: str):
        """工具开始调用"""
        logger.debug(f"[TaskExecutor] 工具调用: {tool_name}")

    def _on_tool_result(self, tool_name: str, tool_id: str, arguments: dict, result: Any):
        """工具结果返回"""
        pass

    def _on_messages_updated(self, messages: list):
        """消息更新"""
        pass

    def _create_session(
        self,
        session_manager: Any,
        config: TaskConfig,
        project: str
    ) -> Optional[str]:
        """创建任务会话
        
        Args:
            session_manager: SessionManager 实例
            config: 任务配置
            project: 项目名称
            
        Returns:
            session_id 或 None
        """
        session_mode = config.context.session_mode
        
        if session_mode == SessionMode.FROM_SESSION_ID:
            session_id = config.context.session_id
            if session_id and self._session_exists(session_manager, session_id):
                logger.debug(f"[TaskExecutor] 使用已有会话: {session_id}")
                return session_id
            else:
                logger.warning(f"[TaskExecutor] 会话不存在: {session_id}，创建新会话")
        
        # 创建新会话
        try:
            session_name = config.name or f"Task_{config.id[:8]}"
            
            if hasattr(session_manager, 'create_new_session'):
                session = session_manager.create_new_session()
                session_id = session.session_id
            elif hasattr(session_manager, 'create_session'):
                session_id = session_manager.create_session(session_name)
            else:
                session_id = None
            
            if session_id:
                logger.debug(f"[TaskExecutor] 创建新会话: {session_id}, project={project}")
            
            # 注意：这里不需要显式设置 project，会话会保存到当前活动的 project
            # 实际 project 设置由调用方在保存时指定
            
            return session_id
        except Exception as e:
            logger.error(f"[TaskExecutor] 创建会话失败: {e}")
            return None

    def _session_exists(self, session_manager: Any, session_id: str) -> bool:
        """检查会话是否存在"""
        if hasattr(session_manager, 'session_exists'):
            return session_manager.session_exists(session_id)
        if hasattr(session_manager, 'get_session'):
            return session_manager.get_session(session_id) is not None
        return False

    def _inject_task_content(
        self,
        session_manager: Any,
        session_id: str,
        config: TaskConfig
    ) -> None:
        """将任务内容注入会话
        
        Args:
            session_manager: SessionManager 实例
            session_id: 会话 ID
            config: 任务配置
        """
        try:
            # 获取会话
            session = None
            if hasattr(session_manager, 'get_session'):
                session = session_manager.get_session(session_id)
            elif hasattr(session_manager, 'get_current_session'):
                session = session_manager.get_current_session()
            
            if not session:
                logger.warning(f"[TaskExecutor] 会话不存在: {session_id}")
                return
            
            # 构建任务内容
            content = self._build_task_message(config)
            
            # 添加用户消息到会话
            if hasattr(session, 'add_user_message'):
                session.add_user_message(content=content, params={})
            elif hasattr(session, 'messages'):
                session.messages.append({
                    "role": "user",
                    "content": content,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            
            logger.debug(f"[TaskExecutor] 注入任务内容到会话: {session_id}")
        except Exception as e:
            logger.error(f"[TaskExecutor] 注入任务内容失败: {e}")

    def _build_task_message(self, config: TaskConfig) -> str:
        """构建任务消息
        
        Args:
            config: 任务配置
            
        Returns:
            任务消息内容
        """
        parts = []
        
        # 添加参考文件
        if config.context.reference_files:
            parts.append("## 参考文件")
            for file_path in config.context.reference_files:
                parts.append(f"- `{file_path}`")
            parts.append("")
        
        # 添加任务内容
        if config.content:
            parts.append("## 任务")
            parts.append(config.content)
        
        return "\n".join(parts)

    def _send_to_engine(
        self,
        chat_engine: Any,
        session_manager: Any,
        session_id: str,
        config: TaskConfig
    ) -> bool:
        """发送消息到 ChatEngine
        
        Args:
            chat_engine: ChatEngine 实例
            session_manager: SessionManager 实例
            session_id: 会话 ID
            config: 任务配置
            
        Returns:
            是否成功
        """
        try:
            # 设置当前 Agent
            if hasattr(chat_engine, 'switch_agent'):
                chat_engine.switch_agent(config.context.agent)
            
            # 切换到对应会话
            self._switch_to_session(session_manager, session_id)
            
            # 发送消息
            if hasattr(chat_engine, 'send_message'):
                return chat_engine.send_message(
                    user_text=config.content or "请执行任务",
                    context_params={"agent": config.context.agent}
                )
            else:
                logger.error("[TaskExecutor] ChatEngine 没有 send_message 方法")
                return False
                
        except Exception as e:
            logger.error(f"[TaskExecutor] 发送消息失败: {e}")
            return False

    def _switch_to_session(self, session_manager: Any, session_id: str) -> bool:
        """切换到指定会话
        
        Args:
            session_manager: SessionManager 实例
            session_id: 会话 ID
            
        Returns:
            是否成功
        """
        try:
            if hasattr(session_manager, 'switch_to_session_by_id'):
                return session_manager.switch_to_session_by_id(session_id)
            
            if hasattr(session_manager, 'get_all_sessions'):
                sessions = session_manager.get_all_sessions()
                for i, s in enumerate(sessions):
                    if hasattr(s, 'session_id') and s.session_id == session_id:
                        if hasattr(session_manager, 'switch_to_session'):
                            session_manager.switch_to_session(i)
                        return True
            
            return False
        except Exception as e:
            logger.error(f"[TaskExecutor] 切换会话失败: {e}")
            return False

    def _handle_error(self, error: str):
        """处理执行错误"""
        if self._current_task:
            config = self._current_task
            execution_time = time.time() - (self._task_start_time or time.time())
            
            logger.error(f"[TaskExecutor] 任务执行失败: {config.id}, error={error}")
            self._emit("task_failed", config, error)
            
            # 记录执行日志
            self._log_execution(config, self._current_session_id, "failed", error_msg=error)
            
            # 构建失败结果
            result = TaskResult(
                success=False,
                task_id=config.id,
                session_id=self._current_session_id,
                error=error,
                execution_time=execution_time
            )
            
            self._emit("task_completed", config, result)
        
        # 释放引擎
        self._release_current_engine()
        
        # 重置状态
        self._reset_state()

    def _release_current_engine(self) -> None:
        """释放当前引擎"""
        if self._current_engine_id:
            self._scheduler.release_engine(self._current_engine_id)

    def _reset_state(self) -> None:
        """重置状态"""
        self._current_task = None
        self._current_engine_id = None
        self._current_session_id = None
        self._current_project = None
        self._collected_content = []

    def on_chat_finished(self, full_content: str):
        """当 ChatEngine 流结束时调用此方法
        
        Args:
            full_content: 完整的回复内容
        """
        if not self._current_task:
            return
        
        config = self._current_task
        execution_time = time.time() - (self._task_start_time or time.time())
        
        logger.info(f"[TaskExecutor] 任务完成: {config.id}, 耗时: {execution_time:.2f}s")
        
        # 合并收集的内容
        if not full_content and self._collected_content:
            full_content = "".join(self._collected_content)
        
        # 记录执行日志
        self._log_execution(config, self._current_session_id, "completed", full_content)
        
        # 构建结果
        result = TaskResult(
            success=True,
            task_id=config.id,
            session_id=self._current_session_id,
            output_content=full_content,
            execution_time=execution_time
        )
        
        self._emit("task_completed", config, result)
        
        # 释放引擎
        self._release_current_engine()
        
        # 重置状态
        self._reset_state()

    def _log_execution(
        self,
        config: TaskConfig,
        session_id: Optional[str],
        status: str,
        result_summary: Optional[str] = None,
        error_msg: Optional[str] = None
    ) -> None:
        """记录执行日志"""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self._db.execute(
                """
                INSERT INTO task_execution_logs 
                (task_id, session_id, started_at, completed_at, status, result_summary)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (config.id, session_id, now, now, status, result_summary or error_msg)
            )
            self._db.commit()
        except Exception as e:
            logger.error(f"[TaskExecutor] 记录执行日志失败: {e}")

    def cancel(self) -> bool:
        """取消当前任务"""
        if not self._current_task:
            return False
        
        try:
            # 尝试取消 ChatEngine
            # 注意：ChatEngine 可能没有 cancel 方法
            self._handle_error("任务被取消")
            return True
        except Exception as e:
            logger.error(f"[TaskExecutor] 取消任务失败: {e}")
            return False

    @property
    def is_running(self) -> bool:
        """是否正在执行"""
        return self._current_task is not None

    @property
    def current_task(self) -> Optional[TaskConfig]:
        """当前任务配置"""
        return self._current_task

    @property
    def scheduler(self) -> EngineScheduler:
        """获取调度器"""
        return self._scheduler

    def get_execution_logs(
        self,
        task_id: Optional[str] = None,
        limit: int = 100
    ) -> list:
        """获取执行日志"""
        try:
            if task_id:
                rows = self._db.fetch_all(
                    "SELECT * FROM task_execution_logs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                    (task_id, limit)
                )
            else:
                rows = self._db.fetch_all(
                    "SELECT * FROM task_execution_logs ORDER BY started_at DESC LIMIT ?",
                    (limit,)
                )
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[TaskExecutor] 获取执行日志失败: {e}")
            return []