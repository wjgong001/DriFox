# -*- coding: utf-8 -*-
"""
PyQt Signal Adapter - 将事件总线事件转换为 PyQt 信号

用于 UI 模式：ChatEngine 订阅 WorkerEventBus，通过此适配器转发到 PyQt 信号。
Qt 的事件循环会自动处理跨线程信号传递（QueuedConnection）。

使用方式：
    adapter = QtSignalAdapter(event_bus, worker)
    adapter.setup_ui_signals()
    # ChatEngine 现在只订阅 event_bus，不再直接 connect PyQt Signal
"""

from typing import Callable, Dict, Any, Optional
from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

from app.core.workers.worker_event_bus import WorkerEventBus, WorkerEvent


class QtSignalAdapter(QObject):
    """
    PyQt Signal 适配器 - 将 WorkerEventBus 事件转发为 PyQt 信号
    
    继承 QObject 以支持 Qt 信号机制。
    使用 QueuedConnection 自动处理跨线程信号传递。
    """
    
    # 定义所有需要转发的信号
    content_received = pyqtSignal(str)
    reasoning_content_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished_with_content = pyqtSignal(str)
    finished_with_messages = pyqtSignal(list)
    compaction_status_changed = pyqtSignal(dict)
    tool_call_started = pyqtSignal(str, str, dict, str)
    tool_result_received = pyqtSignal(str, str, dict, object)
    question_asked = pyqtSignal(str, str, list, bool)
    permission_approval_requested = pyqtSignal(str, str, dict)
    
    # 内部事件映射：WorkerEvent -> signal name
    _EVENT_TO_SIGNAL = {
        WorkerEvent.CONTENT_RECEIVED: "content_received",
        WorkerEvent.REASONING_RECEIVED: "reasoning_content_received",
        WorkerEvent.FINISHED_WITH_CONTENT: "finished_with_content",
        WorkerEvent.FINISHED_WITH_MESSAGES: "finished_with_messages",
        WorkerEvent.COMPACTION_STATUS: "compaction_status_changed",
        WorkerEvent.TOOL_CALL_STARTED: "tool_call_started",
        WorkerEvent.TOOL_RESULT_RECEIVED: "tool_result_received",
        WorkerEvent.QUESTION_ASKED: "question_asked",
        WorkerEvent.PERMISSION_REQUESTED: "permission_approval_requested",
        WorkerEvent.ERROR: "error_occurred",
    }
    
    def __init__(self, event_bus: WorkerEventBus, worker: Any = None):
        """
        初始化适配器
        
        Args:
            event_bus: Worker 事件总线实例
            worker: 可选，关联的 worker 实例
        """
        super().__init__()
        self._event_bus = event_bus
        self._worker = worker
        self._subscriptions: list = []  # 记录所有订阅，便于清理
    
    def setup_ui_signals(self) -> None:
        """
        设置所有 UI 信号订阅。
        调用此方法后，事件总线的事件会自动转发为 PyQt 信号。
        
        应在 UI 初始化时调用一次。
        """
        # 订阅所有事件
        for event, signal_name in self._EVENT_TO_SIGNAL.items():
            handler = self._make_handler(signal_name)
            self._event_bus.subscribe(event, handler)
            self._subscriptions.append((event, handler))
        
        logger.debug("[QtSignalAdapter] UI signals setup complete")
    
    def _make_handler(self, signal_name: str) -> Callable:
        """
        创建事件处理函数，将事件转发为 PyQt 信号
        
        Args:
            signal_name: 信号名称
            
        Returns:
            处理函数
        """
        def handler(*args, **kwargs):
            try:
                signal = getattr(self, signal_name, None)
                if signal is not None:
                    signal.emit(*args, **kwargs)
            except Exception as e:
                logger.error(f"[QtSignalAdapter] Error emitting {signal_name}: {e}")
        return handler
    
    def cleanup(self) -> None:
        """
        清理所有订阅。
        应在 UI 关闭或切换时调用。
        """
        for event, handler in self._subscriptions:
            self._event_bus.unsubscribe(event, handler)
        self._subscriptions.clear()
        logger.debug("[QtSignalAdapter] Cleanup complete")
    
    def __del__(self):
        """析构时自动清理"""
        self.cleanup()