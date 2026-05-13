# -*- coding: utf-8 -*-
"""
任务文件夹监听器
使用 watchdog 监控文件夹变化，检测到新任务文件自动触发
"""
import os
import time
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from loguru import logger

# 尝试导入 watchdog
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileDeletedEvent

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("[TaskWatcher] watchdog 未安装，无法使用文件夹监听功能")
from .models import TaskConfig
from .parser import TaskParser, TaskParseError


class TaskFileHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
    """任务文件事件处理器"""

    def __init__(
            self,
            pattern: str = "*.task.md",
            callback: Optional[Callable[[str, TaskConfig], None]] = None
    ):
        """初始化事件处理器
        
        Args:
            pattern: 文件匹配模式
            callback: 文件创建时的回调函数
        """
        super().__init__()
        self._pattern = pattern
        self._callback = callback
        self._parser = TaskParser()

        # 防抖处理：记录最近处理的文件
        self._recent_files: Dict[str, float] = {}
        self._debounce_seconds = 1.0

    def _matches_pattern(self, file_path: str) -> bool:
        """检查文件是否匹配模式
        
        Args:
            file_path: 文件路径
            
        Returns:
            是否匹配
        """
        from fnmatch import fnmatch
        filename = os.path.basename(file_path)
        return fnmatch(filename, self._pattern)

    def _is_recently_processed(self, file_path: str) -> bool:
        """检查文件是否最近处理过（防抖）
        
        Args:
            file_path: 文件路径
            
        Returns:
            是否最近处理过
        """
        abs_path = os.path.abspath(file_path)
        current_time = time.time()

        # 清理过期的记录
        self._recent_files = {
            k: v for k, v in self._recent_files.items()
            if current_time - v < self._debounce_seconds * 2
        }

        if abs_path in self._recent_files:
            return True

        self._recent_files[abs_path] = current_time
        return False

    def on_created(self, event):
        """文件创建事件"""
        if event.is_directory:
            return

        file_path = event.src_path
        self._handle_file_event(file_path, "created")

    def on_modified(self, event):
        """文件修改事件"""
        if event.is_directory:
            return

        file_path = event.src_path
        # 对于修改事件，只有当文件符合任务文件特征时才处理
        if self._matches_pattern(file_path):
            self._handle_file_event(file_path, "modified")

    def _handle_file_event(self, file_path: str, event_type: str):
        """处理文件事件
        
        Args:
            file_path: 文件路径
            event_type: 事件类型
        """
        if not self._matches_pattern(file_path):
            return

        if self._is_recently_processed(file_path):
            logger.debug(f"[TaskWatcher] 跳过重复事件: {file_path}")
            return

        logger.info(f"[TaskWatcher] 检测到任务文件: {event_type} {file_path}")

        # 等待文件写入完成
        time.sleep(0.5)

        # 解析任务文件
        try:
            config = self._parser.parse_file(file_path)
            if config:
                logger.info(f"[TaskWatcher] 解析任务成功: {config.id}, name={config.name}")
                if self._callback:
                    self._callback(file_path, config)
            else:
                logger.warning(f"[TaskWatcher] 任务文件解析失败: {file_path}")
        except TaskParseError as e:
            logger.error(f"[TaskWatcher] 任务解析错误: {e}")
        except Exception as e:
            logger.error(f"[TaskWatcher] 处理任务文件失败: {e}")


class TaskWatcher:
    """任务文件夹监听器
    
    使用 watchdog 监控文件夹变化
    """

    def __init__(self, config_store=None):
        """初始化任务监听器
        
        Args:
            config_store: 可选，为保持向后兼容
        """
        self._config_store = config_store
        
        # 获取任务目录
        if config_store and hasattr(config_store, 'tasks_dir'):
            self._tasks_dir = config_store.tasks_dir
        else:
            from app.core.task_watcher.file_manager import TaskFileManager
            self._tasks_dir = TaskFileManager.get_instance().config_dir
        
        self._observer: Optional[Observer] = None
        self._handlers: Dict[str, TaskFileHandler] = {}
        self._watch_folders: Dict[str, str] = {}  # folder -> pattern
        self._callback: Optional[Callable[[str, TaskConfig], None]] = None
        self._running = False

        if not WATCHDOG_AVAILABLE:
            logger.warning("[TaskWatcher] watchdog 不可用，文件夹监听功能将无法使用")

    def set_callback(self, callback: Callable[[str, TaskConfig], None]) -> None:
        """设置任务回调
        
        Args:
            callback: 回调函数，参数为 (file_path, TaskConfig)
        """
        self._callback = callback

    def add_watch(self, folder: str, pattern: str = "*.task.md") -> bool:
        """添加文件夹监听
        
        Args:
            folder: 文件夹路径
            pattern: 文件匹配模式
            
        Returns:
            是否成功
        """
        if not WATCHDOG_AVAILABLE:
            logger.error("[TaskWatcher] watchdog 不可用，无法添加监听")
            return False

        abs_folder = os.path.abspath(folder)

        if not os.path.exists(abs_folder):
            try:
                os.makedirs(abs_folder, exist_ok=True)
            except Exception as e:
                logger.error(f"[TaskWatcher] 创建文件夹失败: {abs_folder}, {e}")
                return False

        self._watch_folders[abs_folder] = pattern
        logger.debug(f"[TaskWatcher] 添加监听文件夹: {abs_folder}, pattern={pattern}")
        return True

    def remove_watch(self, folder: str) -> bool:
        """移除文件夹监听
        
        Args:
            folder: 文件夹路径
            
        Returns:
            是否成功
        """
        abs_folder = os.path.abspath(folder)
        if abs_folder in self._watch_folders:
            del self._watch_f[abs_folder]
            logger.debug(f"[TaskWatcher] 移除监听文件夹: {abs_folder}")
            return True
        return False

    def load_watches_from_config(self, config_store: Any) -> int:
        """从配置加载所有文件夹监听
        
        Args:
            config_store: TaskConfigStore 实例
            
        Returns:
            添加的监听数量
        """
        count = 0
        # 获取所有 file_change 模式的任务
        from .models import TriggerMode
        configs = config_store.get_by_trigger_mode(TriggerMode.FILE_CHANGE)
        for config in configs:
            if config.trigger.mode.value != "file_change":
                continue
            folder = config.trigger.watch_folder
            pattern = config.trigger.file_pattern or "*.task.md"
            if self.add_watch(folder, pattern):
                count += 1
        logger.info(f"[TaskWatcher] 从配置加载了 {count} 个文件夹监听")
        return count

    def start(self) -> bool:
        """启动所有监听
        
        Returns:
            是否成功
        """
        if self._running:
            logger.debug("[TaskWatcher] 已经在运行中")
            return True

        if not WATCHDOG_AVAILABLE:
            logger.error("[TaskWatcher] 无法启动，watchdog 不可用")
            return False

        if not self._observer and self._watch_folders:
            # 重新创建观察者
            self._observer = Observer()
            for folder, pattern in self._watch_folders.items():
                handler = TaskFileHandler(
                    pattern=pattern,
                    callback=self._on_file_detected
                )
                self._observer.schedule(handler, folder, recursive=False)
            self._observer.start()

        self._running = True
        logger.info("[TaskWatcher] 启动完成")
        return True

    def stop(self) -> bool:
        """停止所有监听
        
        Returns:
            是否成功
        """
        if not self._running:
            return True

        try:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=2)
                self._observer = None

            self._running = False
            logger.info("[TaskWatcher] 停止完成")
            return True
        except Exception as e:
            logger.error(f"[TaskWatcher] 停止失败: {e}")
            return False

    def on_file_created(self, file_path: str) -> Optional[TaskConfig]:
        """手动触发文件创建事件处理
        
        Args:
            file_path: 文件路径
            
        Returns:
            解析的任务配置或 None
        """
        if not os.path.exists(file_path):
            logger.error(f"[TaskWatcher] 文件不存在: {file_path}")
            return None

        try:
            config = self._parser.parse_file(file_path)
            return config
        except Exception as e:
            logger.error(f"[TaskWatcher] 解析文件失败: {e}")
            return None

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    @property
    def watch_folders(self) -> Dict[str, str]:
        """获取当前监听的文件夹"""
        return dict(self._watch_folders)

    def scan_folder(self, folder: str, pattern: str = "*.task.md") -> list:
        """扫描文件夹中的任务文件
        
        Args:
            folder: 文件夹路径
            pattern: 文件匹配模式
            
        Returns:
            任务文件路径列表
        """
        from fnmatch import fnmatch

        files = []
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and fnmatch(entry.name, pattern):
                    files.append(entry.path)
        except Exception as e:
            logger.error(f"[TaskWatcher] 扫描文件夹失败: {e}")

        return files
