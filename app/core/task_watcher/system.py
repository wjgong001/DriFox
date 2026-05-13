# -*- coding: utf-8 -*-
"""
TaskWatcherSystem - 全局单例的任务调度系统

纯文件化架构：
- 任务配置: .drifox/tasks/*.task.md
- 执行结果: .drifox/tasks/任务名_日期_时间_uuid/
- 不使用数据库，所有数据存储固化为文件
"""
import os
import threading
from typing import Optional, Callable, Dict, Any, List

from loguru import logger
from .file_manager import TaskFileManager
from .executor import TaskExecutor
from .parser import TaskParser
from .watcher import TaskWatcher


class TaskWatcherSystem:
    """
    TaskWatcher 全局单例系统
    
    纯文件化架构：
    - 任务配置: *.task.md 文件
    - 执行结果: 任务名_时间_uuid/ 文件夹
    - 不使用数据库
    """

    # 全局单例
    _instance: Optional["TaskWatcherSystem"] = None
    _lock = threading.Lock()

    def __init__(self,
                 get_model_config: Optional[Callable] = None,
                 tool_executor: Optional[Any] = None):
        """
        初始化 TaskWatcher 系统
        
        Args:
            get_model_config: 获取模型配置的回调
            tool_executor: 工具执行器
        """
        self._file_manager = TaskFileManager.get_instance()
        self._executor = TaskExecutor(get_model_config, tool_executor)
        self._parser = TaskParser()
        self._watcher = TaskWatcher(self._file_manager)

        # 回调
        self._callbacks: Dict[str, List[Callable]] = {}
        self._running = False

        # 设置回调
        self._setup_callbacks()

    @classmethod
    def get_instance(cls, get_model_config=None,
                     tool_executor=None) -> "TaskWatcherSystem":
        """获取全局单例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(get_model_config, tool_executor)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        if cls._instance:
            cls._instance.stop()
            cls._instance = None

    def _setup_callbacks(self) -> None:
        """设置内部回调"""
        # 设置文件监听器回调
        self._watcher.set_callback(self._on_file_detected)

        # 设置执行器回调
        self._executor.set_callback('task_started', self._on_task_started)
        self._executor.set_callback('task_completed', self._on_task_completed)
        self._executor.set_callback('task_failed', self._on_task_failed)
        self._executor.set_callback('task_progress', self._on_task_progress)

    # ==================== 生命周期 ====================

    def start(self) -> None:
        """启动系统"""
        if self._running:
            return

        # 启动文件监听
        self._watcher.start()

        self._running = True
        logger.info("[TaskWatcherSystem] 启动完成")

    def stop(self) -> None:
        """停止系统"""
        if not self._running:
            return

        self._watcher.stop()
        self._running = False
        logger.info("[TaskWatcherSystem] 停止完成")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    # ==================== 回调处理 ====================

    def _on_file_detected(self, file_path: str, config: Any) -> None:
        """文件检测回调"""
        logger.info(f"[TaskWatcherSystem] 检测到任务文件: {config.name}")
        self._emit('task_discovered', config)

    def _on_task_started(self, task_id: str, task_name: str) -> None:
        """任务开始"""
        logger.info(f"[TaskWatcherSystem] 任务开始: {task_name}")
        self._emit('task_started', task_id, task_name)

    def _on_task_completed(self, task_id: str, task_name: str) -> None:
        """任务完成"""
        logger.info(f"[TaskWatcherSystem] 任务完成: {task_name}")
        self._emit('task_completed', task_id, task_name)

    def _on_task_failed(self, task_id: str, task_name: str, error: str) -> None:
        """任务失败"""
        logger.error(f"[TaskWatcherSystem] 任务失败: {task_name}, error={error}")
        self._emit('task_failed', task_id, task_name, error)

    def _on_task_progress(self, task_id: str, task_name: str, message: str) -> None:
        """任务进度"""
        self._emit('task_progress', task_id, task_name, message)

    # ==================== 公开接口 ====================

    def set_callback(self, event: str, callback: Callable) -> None:
        """设置回调"""
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    def _emit(self, event: str, *args) -> None:
        """触发回调"""
        for callback in self._callbacks.get(event, []):
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[TaskWatcherSystem] 回调错误: {e}")

    def execute_task(self, config: Any) -> str:
        """
        执行任务
        
        Args:
            config: 任务配置
            
        Returns:
            result_dir: 结果文件夹路径
        """
        return self._executor.execute(config)

    def get_registered_tasks(self) -> List[Dict[str, Any]]:
        """
        获取已注册的任务列表
        
        Returns:
            任务列表
        """
        task_files = self._file_manager.get_task_files()
        result = []
        for f in task_files:
            # 尝试读取任务的内部 ID，失败则用文件路径
            config = self._parser.parse_file(f.path)
            task_id = config.id if config else f.path
            result.append({
                'id': task_id,
                'name': f.task_name,
                'path': f.path,
                'trigger_mode': f.trigger_mode,
            })
        return result

    def get_execution_results(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        获取历史执行结果
        
        Args:
            limit: 返回数量限制
            
        Returns:
            历史记录列表
        """
        return self._file_manager.list_results(limit)

    def get_running_tasks(self) -> Dict[str, Dict[str, Any]]:
        """获取运行中的任务"""
        return self._executor.get_running_tasks()

    def open_results_folder(self) -> bool:
        """打开结果文件夹"""
        return self._file_manager.open_tasks_folder()

    def create_task_file(self, name: str, content: str,
                         trigger_mode: str = "manual") -> str:
        """
        创建任务文件
        
        Args:
            name: 任务名称
            content: 任务内容
            trigger_mode: 触发模式
            
        Returns:
            任务文件路径
        """
        return self._file_manager.create_task_file(name, content, trigger_mode)

    def parse_task_file(self, file_path: str) -> Optional[Any]:
        """
        解析任务文件
        
        Args:
            file_path: 文件路径
            
        Returns:
            任务配置或 None
        """
        return self._parser.parse_file(file_path)

    @property
    def executor(self) -> TaskExecutor:
        """获取执行器"""
        return self._executor

    @property
    def file_manager(self) -> TaskFileManager:
        """获取文件管理器"""
        return self._file_manager

    @property
    def tasks_dir(self) -> str:
        """获取任务文件夹路径"""
        return self._file_manager.tasks_dir