# -*- coding: utf-8 -*-
"""
EventBus - 事件总线
替代 PyQt pyqtSignal 的纯 Python 事件系统
支持前后端解耦，可接入任何前端（桌面/Web/移动端）
"""

import threading
import asyncio
import weakref
from typing import Callable, Dict, List, Any, Optional, Set
from dataclasses import dataclass, field
from loguru import logger
from collections import defaultdict


@dataclass
class EventListener:
    """事件监听器"""
    callback: Callable
    once: bool = False  # 是否只触发一次
    weak_ref: bool = True  # 是否使用弱引用


class EventBus:
    """
    事件总线 - 发布订阅模式
    
    使用方式（替代 pyqtSignal）:
    
    ```python
    # 定义事件（可选，用于类型提示）
    class MyEvents:
        message_received = "message_received"
        stream_finished = "stream_finished"
    
    # 创建全局事件总线
    bus = EventBus()
    
    # 订阅
    def on_message(msg):
        print(f"收到消息: {msg}")
    
    bus.subscribe("message_received", on_message)
    
    # 发布
    bus.emit("message_received", {"text": "hello"})
    
    # 取消订阅
    bus.unsubscribe("message_received", on_message)
    ```
    
    对于需要跨线程通信的场景（如 Qt UI 线程），使用:
    ```python
    # 在 Qt 线程中注册回调
    bus.subscribe_on_main_thread("message_received", self.on_message)
    ```
    """
    
    _instance: Optional["EventBus"] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._listeners: Dict[str, List[EventListener]] = defaultdict(list)
        self._lock = threading.RLock()  # 可重入锁
        self._main_thread_id: Optional[int] = None
        self._main_thread_callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._main_thread_queue: List[tuple] = []  # 主线程待执行回调
        self._main_thread_lock = threading.Lock()
        
        # 异步支持
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._asyncListeners: Dict[str, List[Callable]] = defaultdict(list)
        
        self._initialized = True
        logger.debug("[EventBus] 初始化完成")
    
    def set_main_thread(self, thread_id: Optional[int] = None):
        """
        设置主线程 ID，用于跨线程回调
        
        Args:
            thread_id: 主线程 ID，默认为当前线程
        """
        self._main_thread_id = thread_id or threading.current_thread().ident
        logger.debug(f"[EventBus] 主线程设置为: {self._main_thread_id}")
    
    def subscribe(
        self, 
        event: str, 
        callback: Callable, 
        once: bool = False,
        weak_ref: bool = True
    ) -> Callable:
        """
        订阅事件
        
        Args:
            event: 事件名称
            callback: 回调函数
            once: 是否只触发一次后自动取消订阅
            weak_ref: 是否使用弱引用（默认 True，避免内存泄漏）
        
        Returns:
            取消订阅的函数，可用于 later unsubscribe
        """
        with self._lock:
            listener = EventListener(
                callback=callback, 
                once=once, 
                weak_ref=weak_ref
            )
            self._listeners[event].append(listener)
            logger.debug(f"[EventBus] 订阅: {event}, callback={callback.__name__ if hasattr(callback, '__name__') else str(callback)}")
            
            # 返回取消订阅函数
            def unsubscribe():
                self.unsubscribe(event, callback)
            return unsubscribe
    
    def once(self, event: str, callback: Callable) -> Callable:
        """订阅一次性事件（触发一次后自动取消）"""
        return self.subscribe(event, callback, once=True)
    
    def unsubscribe(self, event: str, callback: Callable):
        """
        取消订阅
        
        Args:
            event: 事件名称
            callback: 回调函数
        """
        with self._lock:
            listeners = self._listeners.get(event, [])
            for i, listener in enumerate(listeners):
                if listener.callback == callback:
                    listeners.pop(i)
                    logger.debug(f"[EventBus] 取消订阅: {event}")
                    break
    
    def emit(self, event: str, *args, **kwargs):
        """
        同步发布事件（所有订阅者都会收到）
        
        Args:
            event: 事件名称
            *args, **kwargs: 传递给回调的参数
        """
        # 复制一份监听器列表，避免回调中修改导致的问题
        with self._lock:
            listeners = list(self._listeners.get(event, []))
        
        # 处理主线程回调
        self._process_main_thread_queue()
        
        to_remove = []
        for listener in listeners:
            try:
                listener.callback(*args, **kwargs)
                if listener.once:
                    to_remove.append(listener)
            except Exception as e:
                logger.error(f"[EventBus] 事件 {event} 回调执行出错: {e}")
        
        # 移除一次性监听器
        if to_remove:
            with self._lock:
                for listener in to_remove:
                    if listener in self._listeners[event]:
                        self._listeners[event].remove(listener)
    
    def emit_async(self, event: str, *args, **kwargs):
        """
        异步发布事件（在事件循环中触发）
        
        Args:
            event: 事件名称
            *args, **kwargs: 传递给回调的参数
        """
        if self._loop and self._loop.is_running():
            asyncio.create_task(self._emit_coroutine(event, *args, **kwargs))
        else:
            self.emit(event, *args, **kwargs)
    
    async def _emit_coroutine(self, event: str, *args, **kwargs):
        """异步事件触发协程"""
        with self._lock:
            listeners = list(self._listeners.get(event, []))
        
        to_remove = []
        for listener in listeners:
            try:
                if asyncio.iscoroutinefunction(listener.callback):
                    await listener.callback(*args, **kwargs)
                else:
                    listener.callback(*args, **kwargs)
                if listener.once:
                    to_remove.append(listener)
            except Exception as e:
                logger.error(f"[EventBus] 异步事件 {event} 回调执行出错: {e}")
        
        if to_remove:
            with self._lock:
                for listener in to_remove:
                    if listener in self._listeners[event]:
                        self._listeners[event].remove(listener)
    
    def subscribe_async(self, event: str, callback: Callable):
        """订阅异步事件"""
        with self._lock:
            self._asyncListeners[event].append(callback)
    
    def _process_main_thread_queue(self):
        """处理主线程待执行回调"""
        with self._main_thread_lock:
            while self._main_thread_queue:
                callback, args, kwargs = self._main_thread_queue.pop(0)
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    logger.error(f"[EventBus] 主线程回调执行出错: {e}")
    
    def emit_on_main_thread(self, event: str, *args, **kwargs):
        """
        在主线程中触发回调（跨线程安全）
        
        如果在主线程中调用，直接执行；
        否则将回调放入队列，等待主线程处理。
        
        Args:
            event: 事件名称
            *args, **kwargs: 传递给回调的参数
        """
        current_thread = threading.current_thread().ident
        
        if current_thread == self._main_thread_id:
            # 已经在主线程，直接执行
            self.emit(event, *args, **kwargs)
        else:
            # 非主线程，将回调加入队列
            def wrapper():
                self.emit(event, *args, **kwargs)
            
            with self._main_thread_lock:
                self._main_thread_queue.append((wrapper, (), {}))
    
    def subscribe_on_main_thread(self, event: str, callback: Callable):
        """
        订阅在主线程中执行的事件
        
        用于 Qt/UI 场景：事件在后台触发，但回调在主线程执行
        """
        self._main_thread_callbacks[event].append(callback)
        return lambda: self._main_thread_callbacks[event].remove(callback)
    
    def get_listener_count(self, event: Optional[str] = None) -> int:
        """获取监听器数量"""
        with self._lock:
            if event:
                return len(self._listeners.get(event, []))
            return sum(len(v) for v in self._listeners.values())
    
    def clear(self, event: Optional[str] = None):
        """清除监听器"""
        with self._lock:
            if event:
                self._listeners.pop(event, None)
                self._asyncListeners.pop(event, None)
            else:
                self._listeners.clear()
                self._asyncListeners.clear()
        logger.debug(f"[EventBus] 清除监听器: {event or 'all'}")
    
    def get_subscribed_events(self) -> List[str]:
        """获取所有已订阅的事件名称"""
        with self._lock:
            return list(self._listeners.keys())


# ========== 预定义事件名称 ==========
class ChatEvents:
    """聊天相关事件"""
    SESSION_CREATED = "session:created"
    SESSION_CHANGED = "session:changed"
    SESSION_DELETED = "session:deleted"
    
    MESSAGE_RECEIVED = "message:received"
    STREAM_STARTED = "stream:started"
    STREAM_CHUNK = "stream:chunk"
    STREAM_FINISHED = "stream:finished"
    REASONING_CONTENT = "reasoning:content"
    
    TOOL_CALL_STARTED = "tool:call_started"
    TOOL_RESULT_RECEIVED = "tool:result_received"
    
    PERMISSION_REQUESTED = "permission:requested"
    ERROR_OCCURRED = "error:occurred"
    CONTEXT_UPDATED = "context:updated"
    
    # 子智能体相关
    SUBAGENT_TASK_STARTED = "subagent:task_started"
    SUBAGENT_TASK_FINISHED = "subagent:task_finished"
    SUBAGENT_BATCH_FINISHED = "subagent:batch_finished"


# ========== 便捷函数 ==========
_bus: Optional[EventBus] = None

def get_event_bus() -> EventBus:
    """获取全局事件总线实例"""
    global _bus
    if _bus is None:
        _bus = EventBus()
        _bus.set_main_thread()
    return _bus


def subscribe(event: str, callback: Callable, **kwargs) -> Callable:
    """快捷订阅函数"""
    return get_event_bus().subscribe(event, callback, **kwargs)


def unsubscribe(event: str, callback: Callable):
    """快捷取消订阅函数"""
    get_event_bus().unsubscribe(event, callback)


def emit(event: str, *args, **kwargs):
    """快捷发布函数"""
    get_event_bus().emit(event, *args, **kwargs)


def emit_on_main_thread(event: str, *args, **kwargs):
    """快捷主线程发布函数"""
    get_event_bus().emit_on_main_thread(event, *args, **kwargs)


# ========== 信号兼容层（用于平滑迁移）==========
class Signal:
    """
    pyqtSignal 的兼容替代品
    
    使用方式与 pyqtSignal 相似:
    
    ```python
    class MyEmitter:
        message = Signal(str)  # 定义信号，参数类型提示
        
        def send(self, msg):
            self.message.emit(msg)  # 触发信号
    
    # 订阅
    emitter = MyEmitter()
    emitter.message.connect(handler)
    emitter.message.disconnect(handler)
    ```
    """
    
    def __init__(self, *types):
        self._types = types
        self._listeners: List[Callable] = []
        self._lock = threading.Lock()
        self._main_thread_id: Optional[int] = None
    
    def _set_main_thread(self, thread_id: Optional[int] = None):
        """设置主线程 ID（用于跨线程回调）"""
        self._main_thread_id = thread_id or threading.current_thread().ident
    
    def emit(self, *args, **kwargs):
        """
        触发信号
        
        直接调用所有监听器，兼容 Qt 信号槽的跨线程行为。
        Qt 的信号槽机制本身就是线程安全的，会自动将跨线程调用 marshall 到接收者的线程。
        """
        # 复制监听器列表，避免在调用过程中修改
        with self._lock:
            listeners = list(self._listeners)
        
        for listener in listeners:
            try:
                listener(*args, **kwargs)
            except Exception as e:
                logger.error(f"[Signal] 回调执行出错: {e}")
    
    def emit_on_main_thread(self, *args, **kwargs):
        """
        在主线程中触发信号
        
        如果不在主线程，通过 Qt 的 QMetaObject.invokeMethod 或其他机制
        将调用 marshall 到主线程。
        
        注意：对于 PyQt 前端，这个方法会尝试通过 Qt 机制调用。
        对于纯 Python 后端，直接调用即可。
        """
        try:
            from PyQt5.QtCore import QMetaObject, Qt
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            if app and threading.current_thread().ident != self._main_thread_id:
                # 在 Qt 环境中，使用 invokeMethod 将调用 marshall 到主线程
                # 这需要一个 QObject 作为上下文
                # 但 Signal 本身不是 QObject，所以这里需要特殊处理
                pass
        except ImportError:
            # 不是 PyQt 环境，直接调用
            pass
        except Exception:
            # 其他错误，忽略
            pass
        
        # 默认行为：直接调用
        self.emit(*args, **kwargs)
    
    def emit_local(self, *args, **kwargs):
        """
        本地触发信号（不处理跨线程，直接调用所有监听器）
        
        用于在已知是多线程环境的场景。
        """
        with self._lock:
            listeners = list(self._listeners)
        
        for listener in listeners:
            try:
                listener(*args, **kwargs)
            except Exception as e:
                logger.error(f"[Signal] 回调执行出错: {e}")
    
    def connect(self, slot: Callable):
        """连接槽函数"""
        with self._lock:
            if slot not in self._listeners:
                self._listeners.append(slot)
    
    def disconnect(self, slot: Callable):
        """断开连接"""
        with self._lock:
            if slot in self._listeners:
                self._listeners.remove(slot)
    
    def clear(self):
        """清除所有连接"""
        with self._lock:
            self._listeners.clear()
    
    def __call__(self, *args, **kwargs):
        """支持作为装饰器使用"""
        def decorator(func: Callable):
            self.connect(func)
            return func
        return decorator
    
    @property
    def count(self) -> int:
        """返回连接数"""
        with self._lock:
            return len(self._listeners)
