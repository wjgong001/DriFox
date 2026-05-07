# -*- coding: utf-8 -*-
"""
PyQt HTTP API 客户端

提供与后端 FastAPI 服务通信的异步接口。
使用 aiohttp 进行 HTTP 请求，支持 SSE 流式响应。

用法示例：
    client = ApiClient(base_url="http://localhost:8000")
    
    # 健康检查
    health = await client.health_check()
    
    # 创建会话
    session = await client.create_session(title="新对话")
    
    # 流式聊天
    async for event in client.chat_stream(session_id, "你好"):
        print(event)
    
    # 停止流式
    await client.stop_chat(stream_id)
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
from loguru import logger


class ApiClientError(Exception):
    """API 客户端异常"""
    pass


class ApiClient:
    """
    PyQt HTTP API 客户端
    
    Attributes:
        base_url: API 服务基础 URL
        timeout: 请求超时时间（秒）
        default_headers: 默认请求头
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = 30.0,
    ):
        """
        初始化 API 客户端
        
        Args:
            base_url: API 服务基础 URL
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self.default_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=self.default_headers,
            )
        return self._session
    
    async def close(self) -> None:
        """关闭客户端 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def __aenter__(self) -> "ApiClient":
        """异步上下文管理器入口"""
        await self._get_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出"""
        await self.close()
    
    # ==================== 辅助方法 ====================
    
    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求
        
        Args:
            method: HTTP 方法 (GET, POST, DELETE, etc.)
            path: 请求路径
            **kwargs: 传递给 aiohttp 的其他参数
        
        Returns:
            解析后的 JSON 响应
        
        Raises:
            ApiClientError: 请求失败时抛出
        """
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        
        try:
            async with session.request(method, url, **kwargs) as response:
                if response.content_type == "text/event-stream":
                    # SSE 响应不应走这里
                    raise ApiClientError("SSE response should use chat_stream()")
                
                data = await response.json()
                
                if not response.ok:
                    detail = data.get("detail", str(response.status))
                    raise ApiClientError(f"HTTP {response.status}: {detail}")
                
                return data
                
        except aiohttp.ClientError as e:
            raise ApiClientError(f"请求失败: {e}") from e
    
    # ==================== 健康检查 ====================
    
    async def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        Returns:
            服务健康状态信息，包含：
            - status: 服务状态 (ok/stopped)
            - running: 是否正在运行
            - address: 服务地址
            - version: 服务版本
        
        Raises:
            ApiClientError: 服务不可用时抛出
        """
        try:
            return await self._request("GET", "/health")
        except ApiClientError:
            raise
        except Exception as e:
            raise ApiClientError(f"健康检查失败: {e}") from e
    
    # ==================== 会话管理 ====================
    
    async def create_session(self, title: str = "") -> Dict[str, Any]:
        """
        创建新会话
        
        Args:
            title: 会话标题（可选）
        
        Returns:
            创建的会话信息，包含：
            - success: 是否成功
            - session: 会话详情
        
        Raises:
            ApiClientError: 创建失败时抛出
        """
        try:
            data = await self._request(
                "POST",
                "/sessions",
                json={"title": title} if title else {},
            )
            
            if not data.get("success"):
                raise ApiClientError(f"创建会话失败: {data.get('message', '未知错误')}")
            
            return data.get("session", {})
            
        except ApiClientError:
            raise
        except Exception as e:
            raise ApiClientError(f"创建会话失败: {e}") from e
    
    async def list_sessions(self) -> List[Dict[str, Any]]:
        """
        列出会话
        
        Returns:
            会话列表
        
        Raises:
            ApiClientError: 请求失败时抛出
        """
        try:
            data = await self._request("GET", "/sessions")
            return data.get("sessions", [])
        except ApiClientError:
            raise
        except Exception as e:
            raise ApiClientError(f"获取会话列表失败: {e}") from e
    
    async def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        Args:
            session_id: 要删除的会话 ID
        
        Returns:
            是否删除成功
        
        Raises:
            ApiClientError: 删除失败时抛出
        """
        try:
            data = await self._request("DELETE", f"/sessions/{session_id}")
            return data.get("success", False)
        except ApiClientError:
            raise
        except Exception as e:
            raise ApiClientError(f"删除会话失败: {e}") from e
    
    # ==================== 流式聊天 ====================
    
    async def chat_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        SSE 流式聊天
        
        Args:
            message: 用户消息（必填）
            session_id: 会话 ID（可选，不提供则创建新会话）
            context: 额外上下文参数（可选）
        
        Yields:
            SSE 事件字典，常见事件类型：
            - started: {"event": "started", "stream_id": "...", "session_id": "..."}
            - content: {"event": "content", "data": {"piece": "..."}}
            - reasoning: {"event": "reasoning", "data": {"reasoning": "..."}}
            - tool_call_started: {"event": "tool_call_started", "data": {...}}
            - tool_result: {"event": "tool_result", "data": {...}}
            - stream_finished: {"event": "stream_finished", "data": {"content": "...", "finished": true}}
            - error: {"event": "error", "data": {"error": "..."}}
            - complete: {"event": "complete", "content": "..."}
        
        Raises:
            ApiClientError: 请求失败时抛出
        """
        session = await self._get_session()
        url = f"{self.base_url}/chat/stream"
        
        payload = {"message": message}
        if session_id:
            payload["session_id"] = session_id
        if context:
            payload["context"] = context
        
        stream_id: Optional[str] = None
        
        try:
            async with session.post(
                url,
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if not response.ok:
                    data = await response.json()
                    detail = data.get("detail", str(response.status))
                    raise ApiClientError(f"HTTP {response.status}: {detail}")
                
                # 解析 SSE 流
                async for line in response.content:
                    line = line.decode("utf-8").strip()
                    
                    if not line.startswith("data: "):
                        continue
                    
                    data_str = line[6:]  # 去掉 "data: " 前缀
                    
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        logger.warning(f"无法解析 SSE 数据: {data_str}")
                        continue
                    
                    # 记录 stream_id
                    if event.get("event") == "started":
                        stream_id = event.get("stream_id")
                    
                    yield event
                    
                    # 流结束或错误时退出
                    if event.get("event") in ("complete", "error"):
                        break
                        
        except aiohttp.ClientError as e:
            raise ApiClientError(f"流式请求失败: {e}") from e
        finally:
            # 保存 stream_id 供 stop_chat 使用
            if stream_id:
                self._last_stream_id = stream_id
    
    async def stop_chat(self, stream_id: Optional[str] = None) -> Dict[str, Any]:
        """
        停止流式输出
        
        Args:
            stream_id: 要停止的流 ID（可选，不提供则停止最近一个流）
        
        Returns:
            停止结果，包含：
            - success: 是否成功
            - message: 结果消息
            - stream_id: 被停止的流 ID
        
        Raises:
            ApiClientError: 请求失败时抛出
        """
        try:
            payload: Dict[str, Any] = {}
            if stream_id:
                payload["stream_id"] = stream_id
            
            return await self._request("POST", "/chat/stop", json=payload)
            
        except ApiClientError:
            raise
        except Exception as e:
            raise ApiClientError(f"停止流式请求失败: {e}") from e
