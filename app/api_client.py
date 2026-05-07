# -*- coding: utf-8 -*-
"""
API 客户端 - 连接到 FastAPI 服务并处理 SSE 流式响应

用于 UI 重构：让 main_widget 通过 API 调用而非直接调用 ChatEngine
"""

import asyncio
import json
import threading
import queue
from typing import Optional, Dict, Any, Callable, List
from loguru import logger


class SSEClient:
    """SSE (Server-Sent Events) 客户端
    
    解析 SSE 格式的流式响应
    """
    
    def __init__(self):
        self._buffer = ""
    
    def parse(self, data: str) -> List[Dict[str, Any]]:
        """解析 SSE 数据，返回事件列表"""
        self._buffer += data
        events = []
        
        # 按行分割
        lines = self._buffer.split("\n")
        self._buffer = lines[-1]  # 保留最后一行（可能不完整）
        
        event = {}
        for line in lines[:-1]:
            if not line.strip():
                # 空行表示事件结束
                if event:
                    events.append(event)
                    event = {}
                continue
            
            if line.startswith("data: "):
                content = line[6:]  # 去掉 "data: " 前缀
                try:
                    event["data"] = json.loads(content)
                except json.JSONDecodeError:
                    event["data"] = content
            elif line.startswith("event: "):
                event["event"] = line[7:]
            elif ": " in line:
                idx = line.index(": ")
                key = line[:idx]
                value = line[idx + 2:]
                event[key] = value
        
        # 检查是否有未完成的事件
        if event and "data" in event:
            # 如果最后一行以换行结尾，则完整
            if data.endswith("\n"):
                events.append(event)
                self._buffer = ""
        
        return events


class ApiClient:
    """API 客户端 - 连接到 FastAPI 服务
    
    特性：
    - 异步 SSE 流式响应处理
    - 线程安全的事件回调
    - 自动重连
    - 向后兼容（可选 API 模式）
    
    SSE 事件类型：
    - started: 流开始
    - content: 内容片段
    - reasoning_content: 思考内容
    - tool_call_started: 工具调用开始
    - tool_result: 工具结果
    - stream_finished: 流结束
    - error: 错误
    """
    
    # 默认配置
    DEFAULT_HOST = "localhost"
    DEFAULT_PORT = 8765
    
    def __init__(self, host: str = None, port: int = None):
        self.host = host or self.DEFAULT_HOST
        self.port = port or self.DEFAULT_PORT
        self.base_url = f"http://{self.host}:{self.port}"
        
        # 回调
        self._callbacks: Dict[str, Callable] = {}
        
        # 状态
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.new_event_loop] = None
        self._sse_client = SSEClient()
        
        # 当前会话 ID
        self._current_session_id: Optional[str] = None
    
    def set_callback(self, event: str, callback: Callable) -> None:
        """设置事件回调
        
        Args:
            event: 事件类型 (started, content, reasoning_content, 
                   tool_call_started, tool_result, stream_finished, error)
            callback: 回调函数，接收对应的事件数据
        """
        self._callbacks[event] = callback
    
    def set_callbacks(self, callbacks: Dict[str, Callable]) -> None:
        """批量设置回调"""
        self._callbacks.update(callbacks)
    
    def _emit(self, event: str, data: Any) -> None:
        """触发回调"""
        if event in self._callbacks:
            try:
                self._callbacks[event](data)
            except Exception as e:
                logger.error(f"[ApiClient] Callback error for {event}: {e}")
    
    @property
    def current_session_id(self) -> Optional[str]:
        """获取当前会话 ID"""
        return self._current_session_id
    
    def set_current_session_id(self, session_id: str) -> None:
        """设置当前会话 ID"""
        self._current_session_id = session_id
    
    def _run_async_loop(self):
        """在独立线程中运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
    
    async def _chat_stream_async(
        self,
        session_id: str,
        message: str,
        context_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """异步发送聊天请求并处理 SSE 流
        
        Args:
            session_id: 会话 ID
            message: 用户消息
            context_params: 额外的上下文参数
        """
        import aiohttp
        
        url = f"{self.base_url}/sessions/{session_id}/chat/stream"
        headers = {"Content-Type": "application/json"}
        payload = {
            "message": message,
            "context": context_params or {},
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, 
                    json=payload, 
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"[ApiClient] HTTP {response.status}: {error_text}")
                        self._emit("error", f"HTTP {response.status}: {error_text}")
                        return
                    
                    # 处理 SSE 流
                    async for line in response.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded or not decoded.startswith("data: "):
                            continue
                        
                        # 解析 SSE 事件
                        events = self._sse_client.parse(decoded + "\n")
                        for event in events:
                            await self._handle_sse_event(event)
                    
        except aiohttp.ClientError as e:
            logger.error(f"[ApiClient] Connection error: {e}")
            self._emit("error", f"连接错误: {e}")
        except asyncio.CancelledError:
            logger.info("[ApiClient] Request cancelled")
        except Exception as e:
            logger.exception(f"[ApiClient] Unexpected error: {e}")
            self._emit("error", f"未知错误: {e}")
    
    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """处理单个 SSE 事件"""
        data = event.get("data", {})
        event_type = data.get("type") or event.get("event", "")
        
        if not event_type:
            return
        
        # 根据事件类型触发对应回调
        if event_type == "started":
            self._emit("started", data)
        elif event_type == "content":
            self._emit("content", data.get("content", ""))
        elif event_type == "reasoning_content":
            self._emit("reasoning_content", data.get("content", ""))
        elif event_type == "tool_call_started":
            self._emit("tool_call_started", {
                "tool_call_id": data.get("tool_call_id", ""),
                "tool_name": data.get("tool_name", ""),
                "arguments": data.get("arguments", {}),
            })
        elif event_type == "tool_result":
            self._emit("tool_result", {
                "tool_call_id": data.get("tool_call_id", ""),
                "tool_name": data.get("tool_name", ""),
                "result": data.get("result", {}),
            })
        elif event_type == "stream_finished":
            self._emit("stream_finished", data.get("response", ""))
        elif event_type == "error":
            self._emit("error", data.get("message", "未知错误"))
        elif event_type == "complete":
            self._emit("complete", data)
    
    def chat_stream(
        self,
        session_id: str,
        message: str,
        context_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发送聊天请求（启动异步流）
        
        Args:
            session_id: 会话 ID
            message: 用户消息
            context_params: 额外的上下文参数
        """
        # 启动事件循环线程（如果尚未启动）
        if self._loop is None or not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
            self._thread.start()
        
        # 在事件循环中调度异步任务
        asyncio.run_coroutine_threadsafe(
            self._chat_stream_async(session_id, message, context_params),
            self._loop,
        )
    
    def stop(self) -> None:
        """停止客户端"""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        import urllib.request
        import urllib.error
        
        try:
            req = urllib.request.Request(
                f"{self.base_url}/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except Exception:
            return False
    
    def ensure_service_running(self, host: str = None, port: int = None) -> bool:
        """确保 API 服务正在运行
        
        Returns:
            True 如果服务正在运行或已成功启动
        """
        if self.is_connected():
            return True
        
        # 尝试启动服务
        try:
            from app.api.api_server import ensure_service_running
            service = ensure_service_running()
            
            # 等待服务启动
            import time
            for _ in range(10):
                time.sleep(0.5)
                if self.is_connected():
                    return True
            
            return False
        except Exception as e:
            logger.error(f"[ApiClient] Failed to start service: {e}")
            return False
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        """获取会话列表"""
        import urllib.request
        import json as json_lib
        
        try:
            req = urllib.request.Request(
                f"{self.base_url}/sessions",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                return json_lib.loads(response.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"[ApiClient] list_sessions failed: {e}")
            return []
    
    def create_session(self, title: str = "") -> Optional[Dict[str, Any]]:
        """创建新会话"""
        import urllib.request
        import json as json_lib
        import urllib.parse
        
        try:
            url = f"{self.base_url}/sessions"
            if title:
                url += f"?title={urllib.parse.quote(title)}"
            
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json_lib.loads(response.read().decode("utf-8"))
                if result.get("id"):
                    self._current_session_id = result["id"]
                return result
        except Exception as e:
            logger.error(f"[ApiClient] create_session failed: {e}")
            return None
    
    def stop_stream(self, stream_id: str = None) -> bool:
        """停止当前流式请求"""
        import urllib.request
        import json as json_lib
        
        try:
            req = urllib.request.Request(
                f"{self.base_url}/chat/stop",
                data=json_lib.dumps({"stream_id": stream_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json_lib.loads(response.read().decode("utf-8"))
                return result.get("success", False)
        except Exception as e:
            logger.error(f"[ApiClient] stop_stream failed: {e}")
            return False


# 全局单例
_api_client_instance: Optional[ApiClient] = None


def get_api_client(host: str = None, port: int = None) -> ApiClient:
    """获取 API 客户端单例"""
    global _api_client_instance
    if _api_client_instance is None:
        _api_client_instance = ApiClient(host, port)
    return _api_client_instance


def reset_api_client() -> None:
    """重置 API 客户端单例（用于测试或重新初始化）"""
    global _api_client_instance
    if _api_client_instance:
        _api_client_instance.stop()
    _api_client_instance = None
