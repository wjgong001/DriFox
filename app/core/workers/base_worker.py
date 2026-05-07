# -*- coding: utf-8 -*-
"""
BaseWorker - 标准线程 Worker 基类
替代 PyQt QThread 的纯 Python 实现
支持前后端解耦，可接入任何前端
"""

import threading
import queue
import time
import atexit
from abc import ABC, abstractmethod
from typing import Optional, Any, Callable, Dict, List
from loguru import logger
from enum import Enum

from app.core.event_bus import EventBus, get_event_bus, Signal


class WorkerState(Enum):
    """Worker 状态"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FINISHED = "finished"


class BaseWorker(ABC, threading.Thread):
    """
    标准线程 Worker 基类
    
    使用方式（替代 QThread）:
    
    ```python
    class MyWorker(BaseWorker):
        # 定义信号（在 __init__ 后定义）
        def __init__(self):
            super().__init__(daemon=True)
            self.progress = Signal(int)  # 进度信号
            self.result = Signal(str)    # 结果信号
            self.error = Signal(str)     # 错误信号
        
        def do_work(self):
            # 执行工作
            self.progress.emit(50)
            return "完成"
    
    # 使用
    worker = MyWorker()
    worker.progress.connect(on_progress)
    worker.start()  # 启动线程
    ```
    
    特点:
    - 继承自 threading.Thread，使用标准 Python 线程
    - 支持暂停/恢复
    - 支持线程安全的事件发布
    - 自动清理
    """
    
    def __init__(
        self, 
        name: Optional[str] = None,
        daemon: bool = True,
        event_bus: Optional[EventBus] = None
    ):
        """
        初始化 Worker
        
        Args:
            name: Worker 名称，用于调试
            daemon: 是否为守护线程
            event_bus: 事件总线实例（可选，默认使用全局实例）
        """
        thread_name = name or f"Worker-{self.__class__.__name__}-{id(self)}"
        super().__init__(name=thread_name, daemon=daemon)
        
        self._state = WorkerState.IDLE
        self._state_lock = threading.Lock()
        
        # 事件总线
        self._event_bus = event_bus or get_event_bus()
        
        # 停止标志
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_needed = threading.Event()
        
        # 回调队列（用于线程间通信）
        self._callback_queue: queue.Queue = queue.Queue()
        self._main_thread_check_interval = 0.05  # 50ms
        
        # 自动注册清理
        self._register_cleanup()
        
        logger.debug(f"[BaseWorker] 创建: {thread_name}")
    
    def _register_cleanup(self):
        """注册线程清理"""
        def cleanup():
            if self.is_alive():
                logger.debug(f"[BaseWorker] 清理线程: {self.name}")
                self.stop()
                self.join(timeout=2.0)
        
        try:
            atexit.register(cleanup)
        except Exception:
            pass
    
    # ========== 状态管理 ==========
    
    @property
    def state(self) -> WorkerState:
        """获取当前状态"""
        with self._state_lock:
            return self._state
    
    def _set_state(self, new_state: WorkerState):
        """设置状态（线程安全）"""
        with self._state_lock:
            self._state = new_state
    
    def is_running(self) -> bool:
        """检查是否正在运行"""
        return self.state == WorkerState.RUNNING
    
    def is_stopped(self) -> bool:
        """检查是否已停止"""
        return self.state == WorkerState.STOPPED
    
    # ========== 生命周期控制 ==========
    
    def run(self):
        """线程主循环（由 Thread.start() 调用）"""
        self._set_state(WorkerState.RUNNING)
        logger.debug(f"[BaseWorker] 启动: {self.name}")
        
        try:
            self.before_start()
            self.do_work()
            self._set_state(WorkerState.FINISHED)
            logger.debug(f"[BaseWorker] 完成: {self.name}")
        except StopIteration:
            # 正常停止
            self._set_state(WorkerState.FINISHED)
            logger.debug(f"[BaseWorker] 正常停止: {self.name}")
        except Exception as e:
            self._set_state(WorkerState.STOPPED)
            logger.error(f"[BaseWorker] 异常: {self.name}, error={e}")
            self.on_error(e)
        finally:
            self.after_finish()
    
    def do_work(self):
        """
        执行工作（子类必须实现）
        
        在此方法中:
        - 执行耗时的后台任务
        - 调用 self.emit_event() 发布事件
        - 定期检查 self.should_stop() 和 self.should_pause()
        """
        raise NotImplementedError("子类必须实现 do_work 方法")
    
    def before_start(self):
        """开始前调用的钩子"""
        pass
    
    def after_finish(self):
        """完成后调用的钩子"""
        pass
    
    def on_error(self, error: Exception):
        """错误处理钩子"""
        pass
    
    # ========== 暂停/恢复 ==========
    
    def pause(self):
        """请求暂停"""
        if self.is_running():
            self._pause_requested.set()
            self._set_state(WorkerState.PAUSED)
            logger.debug(f"[BaseWorker] 暂停: {self.name}")
    
    def resume(self):
        """请求恢复"""
        if self.state == WorkerState.PAUSED:
            self._resume_needed.set()
            self._pause_requested.clear()
            self._set_state(WorkerState.RUNNING)
            logger.debug(f"[BaseWorker] 恢复: {self.name}")
    
    def should_pause(self) -> bool:
        """检查是否请求暂停（可在线程循环中调用）"""
        if self._pause_requested.is_set():
            self._resume_needed.wait()  # 等待恢复信号
            self._resume_needed.clear()
        return False
    
    # ========== 停止控制 ==========
    
    def stop(self):
        """请求停止"""
        self._stop_requested.set()
        self._resume_needed.set()  # 确保暂停的线程也能退出
        logger.debug(f"[BaseWorker] 请求停止: {self.name}")
    
    def should_stop(self) -> bool:
        """检查是否请求停止（可在线程循环中调用）"""
        return self._stop_requested.is_set()
    
    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        等待线程结束
        
        Args:
            timeout: 超时时间（秒）
            
        Returns:
            是否在超时前结束
        """
        super().join(timeout)
        return not self.is_alive()
    
    # ========== 线程间通信 ==========
    
    def emit_event(self, event: str, *args, **kwargs):
        """
        发布事件（线程安全）
        
        如果在非主线程调用，会将事件放入主线程队列；
        如果在主线程调用，直接触发。
        
        Args:
            event: 事件名称
            *args, **kwargs: 事件参数
        """
        self._event_bus.emit_on_main_thread(event, *args, **kwargs)
    
    def queue_callback(self, callback: Callable, *args, **kwargs):
        """
        将回调放入队列，由主线程执行
        
        用于线程安全地更新 UI。
        
        Args:
            callback: 回调函数
            *args, **kwargs: 回调参数
        """
        self._callback_queue.put((callback, args, kwargs))
    
    def process_callbacks(self):
        """
        处理排队的回调（应在主线程调用）
        
        建议在 UI 的事件循环中定期调用。
        """
        while True:
            try:
                callback, args, kwargs = self._callback_queue.get_nowait()
                callback(*args, **kwargs)
            except queue.Empty:
                break
    
    def check_callbacks(self):
        """检查并处理排队的回调（非阻塞）"""
        try:
            callback, args, kwargs = self._callback_queue.get_nowait()
            callback(*args, **kwargs)
            return True
        except queue.Empty:
            return False
    
    # ========== 便捷方法 ==========
    
    def sleep(self, seconds: float):
        """
        可中断的睡眠（支持暂停和停止）
        
        Args:
            seconds: 睡眠时长
        """
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.should_stop():
                return
            self.should_pause()
            time.sleep(0.05)  # 50ms 睡眠粒度
    
    def __enter__(self):
        """上下文管理器入口"""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.stop()
        self.wait(timeout=5.0)
        return False


class WorkerPool:
    """
    Worker 线程池
    
    用于管理多个 Worker 线程的生命周期。
    """
    
    def __init__(self, max_workers: int = 4):
        self._workers: List[BaseWorker] = []
        self._max_workers = max_workers
        self._lock = threading.Lock()
    
    def add(self, worker: BaseWorker):
        """添加 Worker"""
        with self._lock:
            if len(self._workers) >= self._max_workers:
                raise RuntimeError(f"Worker 池已满（最大 {self._max_workers}）")
            self._workers.append(worker)
            worker.start()
    
    def remove(self, worker: BaseWorker):
        """移除 Worker"""
        with self._lock:
            worker.stop()
            if worker in self._workers:
                self._workers.remove(worker)
    
    def stop_all(self, timeout: float = 5.0):
        """停止所有 Worker"""
        with self._lock:
            for worker in self._workers:
                worker.stop()
            
            for worker in self._workers:
                worker.wait(timeout=timeout)
            
            self._workers.clear()
    
    def __len__(self) -> int:
        return len(self._workers)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_all()
        return False
