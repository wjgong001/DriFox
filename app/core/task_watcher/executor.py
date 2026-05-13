# -*- coding: utf-8 -*-
"""
TaskExecutor - 任务执行器

使用纯文件化架构，不依赖数据库：
- 结果写入文件: conversation.md, result.md, error.log
- 复用 ChatEngine，但不保存会话到数据库
"""
import time
import threading
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List

from loguru import logger
from .models import TaskConfig, TaskResult
from .file_manager import TaskFileManager


class TaskExecutor:
    """
    任务执行器
    
    纯文件化架构：
    - 结果统一写入文件
    - 不保存会话到数据库
    - 复用 ChatEngine
    """

    def __init__(self):
        self._file_manager = TaskFileManager.get_instance()
        self._callbacks: Dict[str, Callable] = {}
        self._running_tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def set_callback(self, event: str, callback: Callable) -> None:
        """设置任务事件回调"""
        self._callbacks[event] = callback

    def _emit(self, event: str, *args) -> None:
        """触发事件"""
        callback = self._callbacks.get(event)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[TaskExecutor] 回调错误: {e}")

    def execute(self, config: TaskConfig) -> str:
        """
        执行任务
        
        创建结果目录，但不阻塞执行。
        实际的执行逻辑由外部（如 TaskExecutionEngine）完成。
        
        Args:
            config: 任务配置
            
        Returns:
            result_dir: 结果文件夹路径
        """
        # 创建结果文件夹
        result_dir = self._file_manager.create_result_dir(config.name)

        # 保存任务上下文
        ctx = {
            'config': config,
            'result_dir': result_dir,
            'start_time': time.time(),
            'conversation_lines': [],
            'status': 'running',
        }

        with self._lock:
            self._running_tasks[config.id] = ctx

        logger.info(f"[TaskExecutor] 创建任务执行上下文: {config.name}, result_dir={result_dir}")
        self._emit('task_started', config.id, config.name, config.name)

        return result_dir

    def append_conversation(self, task_id: str, role: str, content: str) -> None:
        """
        追加对话内容（实时写入）
        
        Args:
            task_id: 任务ID
            role: 角色 (user/assistant/system)
            content: 对话内容
        """
        with self._lock:
            if task_id not in self._running_tasks:
                return

            ctx = self._running_tasks[task_id]
            ctx['conversation_lines'].append(f"## {role.capitalize()}\n\n{content}")

        # 实时写入文件
        self._write_conversation_file(task_id)

    def _write_conversation_file(self, task_id: str) -> None:
        """写入对话记录文件"""
        with self._lock:
            if task_id not in self._running_tasks:
                return
            ctx = self._running_tasks[task_id]

        # 获取任务内容
        task_content = getattr(ctx['config'], 'content', '') or ''
        if hasattr(ctx['config'], 'name'):
            task_name = ctx['config'].name
        else:
            task_name = '未知任务'

        self._file_manager.write_conversation(
            ctx['result_dir'],
            task_name,
            task_content,
            ctx['conversation_lines']
        )

    def complete(self, task_id: str, result: str) -> None:
        """
        任务完成
        
        Args:
            task_id: 任务ID
            result: 执行结果
        """
        with self._lock:
            if task_id not in self._running_tasks:
                logger.warning(f"[TaskExecutor] 任务不存在: {task_id}")
                return

            ctx = self._running_tasks[task_id]
            task_name = ctx['config'].name

        # 写入最终结果
        self._file_manager.write_result(ctx['result_dir'], task_name, result, 'completed')

        # 计算执行时间
        elapsed = time.time() - ctx['start_time']

        logger.info(f"[TaskExecutor] 任务完成: {task_name}, 耗时: {elapsed:.1f}s")

        # 清理
        with self._lock:
            del self._running_tasks[task_id]

        self._emit('task_completed', task_id, task_name, result)

    def fail(self, task_id: str, error: str) -> None:
        """
        任务失败
        
        Args:
            task_id: 任务ID
            error: 错误信息
        """
        with self._lock:
            if task_id not in self._running_tasks:
                logger.warning(f"[TaskExecutor] 任务不存在: {task_id}")
                return

            ctx = self._running_tasks[task_id]
            task_name = ctx['config'].name

        # 写入错误日志
        self._file_manager.write_error(ctx['result_dir'], task_name, error)

        elapsed = time.time() - ctx['start_time']

        logger.error(f"[TaskExecutor] 任务失败: {task_name}, 耗时: {elapsed:.1f}s, error={error}")

        # 清理
        with self._lock:
            del self._running_tasks[task_id]

        self._emit('task_failed', task_id, task_name, error)

    def get_running_tasks(self) -> Dict[str, Dict[str, Any]]:
        """获取运行中的任务"""
        with self._lock:
            return self._running_tasks.copy()

    def is_running(self, task_id: str) -> bool:
        """检查任务是否运行中"""
        with self._lock:
            return task_id in self._running_tasks

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        with self._lock:
            ctx = self._running_tasks.get(task_id)
            if not ctx:
                return None

            return {
                'task_id': task_id,
                'task_name': ctx['config'].name,
                'result_dir': ctx['result_dir'],
                'status': ctx['status'],
                'elapsed': time.time() - ctx['start_time'],
            }

    def cancel_task(self, task_id: str) -> bool:
        """
        取消任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功
        """
        with self._lock:
            if task_id not in self._running_tasks:
                return False

            ctx = self._running_tasks[task_id]
            task_name = ctx['config'].name

        # 写入取消标记
        self._file_manager.write_error(
            ctx['result_dir'],
            task_name,
            "任务被用户取消"
        )

        with self._lock:
            del self._running_tasks[task_id]

        logger.info(f"[TaskExecutor] 任务已取消: {task_name}")
        self._emit('task_cancelled', task_id, task_name)

        return True