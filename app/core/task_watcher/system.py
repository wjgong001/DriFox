# -*- coding: utf-8 -*-
"""
TaskWatcher 系统门面
整合所有组件，提供统一的入口
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List
from loguru import logger

from .database import Database, DRIFOX_DIR
from .config_store import TaskConfigStore
from .parser import TaskParser, TaskParseError
from .queue import TaskQueue
from .watcher import TaskWatcher
from .scheduler import TaskScheduler
from .output_handler import OutputHandler
from .engine_scheduler import get_engine_scheduler, EngineScheduler
from .models import TaskConfig, TaskResult, TriggerMode, QueueStatus
from .task_execution_engine import TaskExecutionEngine, get_task_execution_engine


class TaskWatcherSystem:
    """TaskWatcher 系统门面
    
    整合所有组件，提供统一的入口
    使用独立的 TaskExecutionEngine 执行任务，不复用现有 ChatEngine
    """

    # 默认任务项目名称
    DEFAULT_TASK_PROJECT = "任务执行"
    
    def __init__(
        self,
        scheduler: Optional[EngineScheduler] = None,
        event_bus: Any = None,
        db_path: Optional[str] = None,
        main_widget=None,
    ):
        """初始化 TaskWatcher 系统
        
        Args:
            scheduler: 引擎调度器（默认使用全局调度器）
            event_bus: 事件总线（可选）
            db_path: 数据库路径（可选）
            main_widget: UI 主窗口（用于创建隔离的执行环境）
        """
        # 配置 - 使用项目本地目录
        self._watch_root: Optional[str] = os.path.join(DRIFOX_DIR, "tasks")
        self._main_widget = main_widget
        
        # 初始化数据库
        self._db = Database.get_instance(db_path)
        
        # 初始化引擎调度器（备用）
        self._engine_scheduler = scheduler or get_engine_scheduler()
        self._engine_scheduler.set_default_project(self.DEFAULT_TASK_PROJECT)
        
        # 初始化各组件
        self._config_store = TaskConfigStore(tasks_dir=self._watch_root)
        self._queue = TaskQueue(self._db)
        self._output_handler = OutputHandler()
        self._watcher = TaskWatcher(self._config_store)
        self._scheduler = TaskScheduler(self._config_store)
        self._parser = TaskParser()
        
        # 初始化任务执行引擎（使用独立环境）
        self._task_engine = get_task_execution_engine(main_widget)
        
        # 系统状态
        self._running = False
        self._processing = False
        
        # 回调
        self._callbacks: Dict[str, Callable] = {}
        
        # 任务完成回调映射
        self._pending_tasks: Dict[str, int] = {}  # task_id -> queue_id
        
        # 设置回调
        self._setup_callbacks()
    
    @property
    def task_engine(self) -> TaskExecutionEngine:
        """获取任务执行引擎"""
        return self._task_engine

    def _setup_callbacks(self) -> None:
        """设置内部回调"""
        # 设置调度器回调
        self._scheduler.set_callback(self._on_scheduled_trigger)
        
        # 设置监听器回调
        self._watcher.set_callback(self._on_file_detected)
        
        # 设置任务执行引擎回调
        self._task_engine.set_callback("task_started", self._on_task_started)
        self._task_engine.set_callback("task_completed", self._on_task_completed)
        self._task_engine.set_callback("task_failed", self._on_task_failed)
        self._task_engine.set_callback("task_cancelled", self._on_task_cancelled)

    def _on_scheduled_trigger(self, config: TaskConfig) -> None:
        """定时任务触发回调
        
        Args:
            config: 任务配置
        """
        logger.info(f"[TaskWatcherSystem] 定时任务触发: {config.id}")
        self.enqueue_task(config, trigger_type="scheduled")

    def _on_file_detected(self, file_path: str, config: TaskConfig) -> None:
        """文件检测回调
        
        Args:
            file_path: 文件路径
            config: 任务配置
        """
        logger.info(f"[TaskWatcherSystem] 检测到任务文件: {file_path}")
        self.enqueue_task(config, trigger_type="file_change", source_file=file_path)

    def _on_task_started(self, task_id: str, task_name: str, project: str) -> None:
        """任务开始回调"""
        # 找到对应的 config（从 pending_tasks 中查找）
        config = None
        for tid, qid in self._pending_tasks.items():
            if tid == task_id:
                config = self._config_store.get(task_id)
                break
        
        if config:
            self._emit("task_started", config)
        logger.debug(f"[TaskWatcherSystem] 任务开始: {task_id}, project={project}")

    def _on_task_completed(self, config: TaskConfig, result: TaskResult) -> None:
        """任务完成回调
        
        Args:
            config: 任务配置
            result: 执行结果
        """
        # 更新队列状态
        queue_id = self._pending_tasks.pop(config.id, None)
        if queue_id:
            if result.success:
                self._queue.update_status(queue_id, QueueStatus.COMPLETED)
            else:
                # 获取任务配置用于检查重试次数
                task_config = self._config_store.get(config.id)
                if task_config and result.execution_time is not None:
                    # 获取队列项检查重试次数
                    items = self._queue.get_by_task_id(config.id)
                    for item in items:
                        if item.id == queue_id and item.retry_count < task_config.retry:
                            self._queue.increment_retry(queue_id)
                            self._queue.update_status(queue_id, QueueStatus.PENDING, result.error)
                            break
                    else:
                        self._queue.update_status(queue_id, QueueStatus.FAILED, result.error)
                else:
                    self._queue.update_status(queue_id, QueueStatus.FAILED, result.error)
        
        self._emit("task_completed", config, result)
        logger.debug(f"[TaskWatcherSystem] 任务完成: {config.id}, success={result.success}")
        
        # 处理输出
        self._output_handler.handle(config, result)
        
        # 对于 file_change 模式，删除源文件
        if config.trigger.mode == TriggerMode.FILE_CHANGE and config.source_file:
            self._cleanup_source_file(config.source_file)
        
        # 继续处理队列
        self._process_queue()

    def _on_task_failed(self, config: TaskConfig, error: str) -> None:
        """任务失败回调
        
        Args:
            config: 任务配置
            error: 错误信息
        """
        queue_id = self._pending_tasks.pop(config.id, None)
        if queue_id:
            self._queue.update_status(queue_id, QueueStatus.FAILED, error)
        
        self._emit("task_failed", config, error)
        logger.error(f"[TaskWatcherSystem] 任务失败: {config.id}, error={error}")
        
        # 继续处理队列
        self._process_queue()

    def _on_task_cancelled(self, task_id: str) -> None:
        """任务取消回调
        
        Args:
            task_id: 任务 ID
        """
        queue_id = self._pending_tasks.pop(task_id, None)
        if queue_id:
            self._queue.update_status(queue_id, QueueStatus.FAILED, "任务已取消")
        
        self._emit("task_cancelled", task_id)
        logger.info(f"[TaskWatcherSystem] 任务已取消: {task_id}")
        
        # 继续处理队列
        self._process_queue()

    def _cleanup_source_file(self, file_path: str) -> None:
        """清理源文件
        
        Args:
            file_path: 文件路径
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"[TaskWatcherSystem] 已删除源文件: {file_path}")
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 删除源文件失败: {e}")

    def _emit(self, event: str, *args) -> None:
        """触发事件"""
        callback = self._callbacks.get(event)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[TaskWatcherSystem] 回调错误 {event}: {e}")

    # ========== 公共接口 ==========

    def set_callback(self, event: str, callback: Callable) -> None:
        """设置事件回调
        
        Args:
            event: 事件名 (task_started/task_completed/task_failed)
            callback: 回调函数
        """
        self._callbacks[event] = callback

    def start(self, watch_root: Optional[str] = None) -> bool:
        """启动系统
        
        Args:
            watch_root: 监听根目录（可选）
            
        Returns:
            是否成功
        """
        if self._running:
            logger.debug("[TaskWatcherSystem] 已经在运行中")
            return True
        
        logger.info("[TaskWatcherSystem] 启动 TaskWatcher 系统...")
        
        try:
            # 设置监听根目录
            if watch_root:
                self._watch_root = os.path.abspath(os.path.expanduser(watch_root))
            
            # 确保目录存在
            os.makedirs(self._watch_root, exist_ok=True)
            
            # 启动调度器
            self._scheduler.load_scheduled_tasks(self._config_store)
            self._scheduler.start()
            
            # 启动文件夹监听
            self._watcher.load_watches_from_config(self._config_store)
            self._watcher.start()
            
            # 扫描现有任务文件
            self._scan_existing_tasks()
            
            self._running = True
            logger.info(f"[TaskWatcherSystem] 启动完成，监听目录: {self._watch_root}")
            return True
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 启动失败: {e}")
            return False

    def stop(self) -> bool:
        """停止系统
        
        Returns:
            是否成功
        """
        if not self._running:
            return True
        
        logger.info("[TaskWatcherSystem] 停止 TaskWatcher 系统...")
        
        try:
            # 停止调度器
            self._scheduler.stop()
            
            # 停止监听器
            self._watcher.stop()
            
            self._running = False
            logger.info("[TaskWatcherSystem] 停止完成")
            return True
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 停止失败: {e}")
            return False

    def _scan_existing_tasks(self) -> int:
        """扫描现有任务文件
        
        注意：不保存到数据库，直接返回文件数量
        文件系统是唯一的任务来源，删除文件即删除任务
        
        Returns:
            扫描到的任务数量
        """
        count = 0
        
        if not os.path.exists(self._watch_root):
            logger.debug(f"[TaskWatcherSystem] 任务目录不存在: {self._watch_root}")
            return count
        
        for root, dirs, files in os.walk(self._watch_root):
            for file in files:
                if file.endswith(".task.md"):
                    count += 1
        
        logger.info(f"[TaskWatcherSystem] 扫描到 {count} 个任务文件")
        return count

    def enqueue_task(
        self,
        config: TaskConfig,
        trigger_type: str = "manual",
        source_file: Optional[str] = None
    ) -> int:
        """将任务加入队列
        
        Args:
            config: 任务配置
            trigger_type: 触发类型
            source_file: 源文件路径（用于 file_change 模式）
            
        Returns:
            队列项 ID
        """
        if source_file:
            config.source_file = source_file
        
        # 保存配置
        self._config_store.save(config)
        
        # 入队
        queue_id = self._queue.enqueue(config, trigger_type)
        
        logger.debug(f"[TaskWatcherSystem] 任务入队: {config.id}, queue_id={queue_id}")
        
        # 触发处理
        self._process_queue()
        
        return queue_id

    def execute_now(self, file_path: str, callback: Optional[Callable[[TaskResult], None]] = None) -> None:
        """立即执行任务文件
        
        Args:
            file_path: 任务文件路径
            callback: 完成回调（可选）
        """
        try:
            config = self._parser.parse_file(file_path)
            if not config:
                logger.error(f"[TaskWatcherSystem] 解析任务文件失败: {file_path}")
                if callback:
                    callback(TaskResult(success=False, task_id="", error="解析失败"))
                return
            
            config.source_file = file_path
            
            # 直接执行（使用 TaskExecutionEngine）
            self._pending_tasks[config.id] = -1  # -1 表示不在队列中
            
            project = config.context.get("project") if config.context else None
            agent = config.context.get("agent", "plan") if config.context else "plan"
            
            def on_result(result: str, success: bool):
                task_result = TaskResultModel(
                    success=success,
                    task_id=config.id,
                    output_content=result if success else None,
                    error=None if success else result,
                )
                self._pending_tasks.pop(config.id, None)
                if callback:
                    callback(task_result)
            
            self._task_engine.execute_task(
                task_id=config.id,
                task_name=config.name or "未命名任务",
                task_content=config.content or "",
                project=project or self.DEFAULT_TASK_PROJECT,
                agent=agent,
                callback=on_result,
            )
            
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 立即执行失败: {e}")
            if callback:
                callback(TaskResult(success=False, task_id="", error=str(e)))

    def trigger_task(self, task_id: str) -> bool:
        """手动触发任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        config = self._config_store.get(task_id)
        if not config:
            logger.warning(f"[TaskWatcherSystem] 任务不存在: {task_id}")
            return False
        
        self.enqueue_task(config, trigger_type="manual")
        return True

    def _process_queue(self) -> None:
        """处理队列（事件驱动模式）"""
        if self._processing:
            return
        
        self._processing = True
        
        try:
            # 出队
            item = self._queue.dequeue()
            if not item:
                self._processing = False
                return
            
            # 获取任务配置
            config = self._config_store.get(item.task_id)
            if not config:
                logger.warning(f"[TaskWatcherSystem] 任务配置不存在: {item.task_id}")
                self._queue.update_status(item.id, QueueStatus.FAILED, "配置不存在")
                self._processing = False
                return
            
            # 更新状态为运行中
            self._queue.update_status(item.id, QueueStatus.RUNNING)
            
            # 记录 pending 任务
            self._pending_tasks[config.id] = item.id
            
            # 使用独立的 TaskExecutionEngine 执行任务
            project = config.context.project if hasattr(config.context, 'project') else None
            agent = config.context.agent if hasattr(config.context, 'agent') else "plan"
            
            self._task_engine.execute_task(
                task_id=config.id,
                task_name=config.name or "未命名任务",
                task_content=config.content or "",
                project=project or self.DEFAULT_TASK_PROJECT,
                agent=agent,
                callback=lambda result, success: self._on_task_result(config, result, success),
            )
            
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 处理队列失败: {e}")
        finally:
            # 立即重置，避免阻塞后续任务
            self._processing = False
    
    def _on_task_result(self, config: TaskConfig, result: str, success: bool) -> None:
        """任务执行结果回调"""
        from .models import TaskResult as TaskResultModel
        
        queue_id = self._pending_tasks.pop(config.id, None)
        
        task_result = TaskResultModel(
            success=success,
            task_id=config.id,
            output_content=result if success else None,
            error=None if success else result,
        )
        
        if queue_id:
            self._queue.update_status(
                queue_id,
                QueueStatus.COMPLETED if success else QueueStatus.FAILED,
                task_result.error
            )
        
        self._emit("task_completed", config, task_result)
        logger.debug(f"[TaskWatcherSystem] 任务完成: {config.id}, success={success}")

    # ========== 配置管理 ==========

    def add_task_from_file(self, file_path: str) -> Optional[TaskConfig]:
        """从文件添加任务
        
        Args:
            file_path: 任务文件路径
            
        Returns:
            任务配置或 None
        """
        try:
            config = self._parser.parse_file(file_path)
            if config:
                self._config_store.save(config)
                logger.info(f"[TaskWatcherSystem] 添加任务: {config.id}, name={config.name}")
            return config
        except Exception as e:
            logger.error(f"[TaskWatcherSystem] 添加任务失败: {e}")
            return None

    def remove_task(self, task_id: str) -> bool:
        """删除任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        # 取消定时调度
        self._scheduler_component.unschedule_task(task_id)
        
        # 删除配置
        return self._config_store.delete(task_id)

    def enable_task(self, task_id: str) -> bool:
        """启用任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        result = self._config_store.enable(task_id)
        if result:
            # 重新调度
            config = self._config_store.get(task_id)
            if config and config.trigger.mode == TriggerMode.SCHEDULED:
                self._scheduler_component.schedule_task(config)
        return result

    def disable_task(self, task_id: str) -> bool:
        """禁用任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        result = self._config_store.disable(task_id)
        if result:
            # 取消调度
            self._scheduler_component.unschedule_task(task_id)
        return result

    def get_all_tasks(self) -> List[TaskConfig]:
        """获取所有任务
        
        Returns:
            任务配置列表
        """
        return self._config_store.load_all()

    def get_task(self, task_id: str) -> Optional[TaskConfig]:
        """获取任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            任务配置或 None
        """
        return self._config_store.get(task_id)

    # ========== 状态查询 ==========

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    @property
    def pending_count(self) -> int:
        """待处理任务数量"""
        return self._queue.get_pending_count()

    @property
    def running_count(self) -> int:
        """运行中任务数量"""
        return len(self._pending_tasks)

    def get_queue_stats(self) -> Dict[str, int]:
        """获取队列统计
        
        Returns:
            统计信息
        """
        return {
            "pending": self._queue.get_pending_count(),
            "running": len(self._pending_tasks),
            "scheduled": self._scheduler.scheduled_count if hasattr(self._scheduler, 'scheduled_count') else 0,
        }

    def get_execution_logs(self, task_id: Optional[str] = None, limit: int = 100) -> list:
        """获取执行日志
        
        Args:
            task_id: 任务 ID（可选）
            limit: 返回数量限制
            
        Returns:
            执行日志列表
        """
        return self._executor.get_execution_logs(task_id, limit)

    # ========== 监听文件夹管理 ==========

    def add_watch_folder(self, folder: str, pattern: str = "*.task.md") -> bool:
        """添加监听文件夹
        
        Args:
            folder: 文件夹路径
            pattern: 文件匹配模式
            
        Returns:
            是否成功
        """
        return self._watcher.add_watch(folder, pattern)

    def remove_watch_folder(self, folder: str) -> bool:
        """移除监听文件夹
        
        Args:
            folder: 文件夹路径
            
        Returns:
            是否成功
        """
        return self._watcher.remove_watch(folder)

    @property
    def watch_folders(self) -> Dict[str, str]:
        """获取监听的文件夹"""
        return self._watcher.watch_folders

    # ========== 工具方法 ==========

    def create_task_file(
        self,
        name: str,
        content: str,
        trigger_mode: str = "manual",
        output_path: Optional[str] = None,
        agent: str = "plan",
        folder: Optional[str] = None
    ) -> str:
        """创建任务文件
        
        Args:
            name: 任务名称
            content: 任务内容
            trigger_mode: 触发模式
            output_path: 输出路径
            agent: 使用的 Agent
            folder: 保存到的文件夹（默认使用监听根目录）
            
        Returns:
            创建的文件路径
        """
        import uuid
        
        # 确保 watch_root 已初始化
        if not self._watch_root:
            self._watch_root = os.path.join(DRIFOX_DIR, "tasks")
        
        # 确定保存文件夹
        if not folder:
            folder = self._watch_root
        
        # 确保文件夹存在
        os.makedirs(folder, exist_ok=True)
        
        # 生成任务文件
        task_id = str(uuid.uuid4())
        filename = f"{name.replace(' ', '_')}_{task_id[:8]}.task.md"
        file_path = os.path.join(folder, filename)
        
        # 构建内容
        lines = [
            "---",
            f"id: {task_id}",
            f"name: {name}",
            "type: custom",
            "trigger:",
            f"  mode: {trigger_mode}",
            "context:",
            f"  session_mode: new",
            f"  agent: {agent}",
            "output:",
            "  mode: file",
        ]
        
        if output_path:
            lines.append(f"  destination: {output_path}")
        
        lines.append("  format: markdown")
        lines.append("---")
        lines.append("")
        lines.append(content)
        
        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        
        logger.info(f"[TaskWatcherSystem] 创建任务文件: {file_path}")
        return file_path

    def export_tasks_to_folder(self, folder: str) -> int:
        """导出所有任务到文件夹
        
        Args:
            folder: 目标文件夹
            
        Returns:
            导出的任务数量
        """
        os.makedirs(folder, exist_ok=True)
        
        tasks = self.get_all_tasks()
        count = 0
        
        for config in tasks:
            file_path = os.path.join(folder, f"{config.id}.task.md")
            if self._config_store.export_to_file(config.id, file_path):
                count += 1
        
        return count

    def clear_completed_tasks(self, older_than_hours: int = 24) -> int:
        """清理已完成的旧队列项
        
        Args:
            older_than_hours: 保留最近多少小时内完成的任务
            
        Returns:
            清理数量
        """
        return self._queue.clear_completed(older_than_hours)

    def requeue_failed_tasks(self, max_retries: int = 3) -> int:
        """重新入队失败任务
        
        Args:
            max_retries: 最大重试次数
            
        Returns:
            重新入队的数量
        """
        return self._queue.requeue_failed(max_retries)