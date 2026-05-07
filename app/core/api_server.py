# -*- coding: utf-8 -*-
"""
后端 API 服务 - HTTP/WebSocket 接口
用于前后端分离，支持网页版、手机版等不同前端

使用方法:
```python
# 启动服务
from app.core.api_server import create_api_server
server = create_api_server(backend=my_backend, port=8080)
server.start()

# 或使用 FastAPI
from app.core.api_server import create_fastapi_app
app = create_fastapi_app(backend=my_backend)
```
"""

import json
import threading
import asyncio
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, asdict
from enum import Enum
from loguru import logger

from app.core.event_bus import ChatEvents, get_event_bus


class MessageRole(Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class APIMessage:
    """API 消息格式"""
    role: str
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None


@dataclass
class APIStreamChunk:
    """流式响应块"""
    type: str  # "content" | "tool_call" | "error" | "done"
    content: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    arguments: Optional[Dict] = None
    error: Optional[str] = None


@dataclass
class APISession:
    """API 会话格式"""
    session_id: str
    name: str
    message_count: int
    created_at: str
    last_updated: str


class WebSocketManager:
    """
    WebSocket 连接管理器
    
    管理多个前端 WebSocket 连接，广播事件。
    """
    
    def __init__(self):
        self._connections: Dict[str, Any] = {}  # connection_id -> connection
        self._lock = threading.Lock()
        self._next_id = 0
    
    def add_connection(self, connection: Any) -> str:
        """添加 WebSocket 连接"""
        with self._lock:
            conn_id = f"conn_{self._next_id}"
            self._next_id += 1
            self._connections[conn_id] = connection
            logger.info(f"[WebSocketManager] 新连接: {conn_id}")
            return conn_id
    
    def remove_connection(self, conn_id: str):
        """移除 WebSocket 连接"""
        with self._lock:
            if conn_id in self._connections:
                del self._connections[conn_id]
                logger.info(f"[WebSocketManager] 断开连接: {conn_id}")
    
    def send_to(self, conn_id: str, message: Dict):
        """发送消息到指定连接"""
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn:
                try:
                    # 异步发送
                    asyncio.create_task(self._async_send(conn, message))
                except Exception as e:
                    logger.error(f"[WebSocketManager] 发送失败: {e}")
    
    async def _async_send(self, conn: Any, message: Dict):
        """异步发送消息"""
        try:
            await conn.send_json(message)
        except Exception as e:
            logger.error(f"[WebSocketManager] 异步发送失败: {e}")
    
    def broadcast(self, message: Dict, exclude: Optional[str] = None):
        """广播消息到所有连接"""
        with self._lock:
            for conn_id, conn in self._connections.items():
                if conn_id != exclude:
                    try:
                        asyncio.create_task(self._async_send(conn, message))
                    except Exception as e:
                        logger.error(f"[WebSocketManager] 广播失败: {e}")
    
    def get_connection_count(self) -> int:
        """获取连接数"""
        with self._lock:
            return len(self._connections)


class BackendAPI:
    """
    后端 API - 封装 ChatBackend 提供统一接口
    
    前后端通过此接口通信，支持：
    - HTTP REST API
    - WebSocket 实时通信
    - Server-Sent Events (SSE)
    
    示例（前端使用 fetch）：
    ```javascript
    // 发送消息
    const response = await fetch('/api/chat/send', {
        method: 'POST',
        body: JSON.stringify({ text: '你好' })
    });
    
    // 订阅事件（WebSocket）
    const ws = new WebSocket('ws://localhost:8080/ws');
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'stream_chunk') {
            console.log('收到:', data.content);
        }
    };
    ```
    """
    
    def __init__(self, backend, event_bus=None):
        """
        初始化 Backend API
        
        Args:
            backend: ChatBackend 实例
            event_bus: 事件总线（可选）
        """
        self._backend = backend
        self._event_bus = event_bus or get_event_bus()
        self._ws_manager = WebSocketManager()
        self._subscriptions: Dict[str, List[Callable]] = {}
        
        # 订阅后端事件
        self._setup_event_subscriptions()
    
    def _setup_event_subscriptions(self):
        """订阅后端事件"""
        # 流式内容
        self._event_bus.subscribe(ChatEvents.STREAM_CHUNK, self._on_stream_chunk)
        self._event_bus.subscribe(ChatEvents.STREAM_FINISHED, self._on_stream_finished)
        
        # 工具调用
        self._event_bus.subscribe(ChatEvents.TOOL_CALL_STARTED, self._on_tool_call)
        self._event_bus.subscribe(ChatEvents.TOOL_RESULT_RECEIVED, self._on_tool_result)
        
        # 错误
        self._event_bus.subscribe(ChatEvents.ERROR_OCCURRED, self._on_error)
        
        # 会话
        self._event_bus.subscribe(ChatEvents.SESSION_CREATED, self._on_session_created)
        self._event_bus.subscribe(ChatEvents.SESSION_CHANGED, self._on_session_changed)
    
    def _on_stream_chunk(self, chunk: str):
        """流式内容事件"""
        self._broadcast({
            "type": "stream_chunk",
            "content": chunk
        })
    
    def _on_stream_finished(self, message: dict):
        """流式完成事件"""
        self._broadcast({
            "type": "stream_finished",
            "message": message
        })
    
    def _on_tool_call(self, tool_call_id: str, tool_name: str, arguments: dict):
        """工具调用开始事件"""
        self._broadcast({
            "type": "tool_call_started",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments
        })
    
    def _on_tool_result(self, tool_call_id: str, tool_name: str, result: dict, success: bool):
        """工具结果事件"""
        self._broadcast({
            "type": "tool_result_received",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
            "success": success
        })
    
    def _on_error(self, error: str):
        """错误事件"""
        self._broadcast({
            "type": "error",
            "error": error
        })
    
    def _on_session_created(self, session_id: str):
        """会话创建事件"""
        self._broadcast({
            "type": "session_created",
            "session_id": session_id
        })
    
    def _on_session_changed(self, session_id: str):
        """会话切换事件"""
        self._broadcast({
            "type": "session_changed",
            "session_id": session_id
        })
    
    def _broadcast(self, message: Dict):
        """广播消息到所有 WebSocket 连接"""
        self._ws_manager.broadcast(message)
    
    # ========== HTTP API ==========
    
    def send_message(self, text: str, agent_name: str = None) -> Dict:
        """
        发送消息
        
        POST /api/chat/send
        Body: { "text": "...", "agent_name": "..." }
        """
        self._backend.send_message(text, agent_name=agent_name)
        return {"status": "ok", "message": "Message sent"}
    
    def stop_streaming(self) -> Dict:
        """
        停止流式输出
        
        POST /api/chat/stop
        """
        self._backend.stop_streaming()
        return {"status": "ok"}
    
    def get_sessions(self) -> List[APISession]:
        """
        获取所有会话
        
        GET /api/sessions
        """
        sessions = self._backend.get_all_sessions()
        return [
            APISession(
                session_id=s.session_id,
                name=s.name,
                message_count=s.message_count,
                created_at=s.created_at,
                last_updated=s.last_updated
            )
            for s in sessions
        ]
    
    def get_current_session(self) -> Optional[APISession]:
        """
        获取当前会话
        
        GET /api/sessions/current
        """
        session = self._backend.get_current_session()
        if session:
            return APISession(
                session_id=session.session_id,
                name=session.name,
                message_count=session.message_count,
                created_at=session.created_at,
                last_updated=session.last_updated
            )
        return None
    
    def get_messages(self, session_id: str = None) -> List[APIMessage]:
        """
        获取会话消息
        
        GET /api/sessions/{session_id}/messages
        """
        if session_id:
            # 切换到指定会话
            sessions = self._backend.get_all_sessions()
            for i, s in enumerate(sessions):
                if s.session_id == session_id:
                    self._backend.switch_session(i)
                    break
        
        session = self._backend.get_current_session()
        if session:
            return [
                APIMessage(role=m.get("role"), content=m.get("content"))
                for m in session.messages
            ]
        return []
    
    def create_session(self) -> APISession:
        """
        创建新会话
        
        POST /api/sessions
        """
        session = self._backend.create_session()
        return APISession(
            session_id=session.session_id,
            name=session.name,
            message_count=session.message_count,
            created_at=session.created_at,
            last_updated=session.last_updated
        )
    
    def switch_session(self, index: int) -> Dict:
        """
        切换会话
        
        POST /api/sessions/switch
        Body: { "index": 0 }
        """
        self._backend.switch_session(index)
        return {"status": "ok"}
    
    def delete_session(self, index: int) -> Dict:
        """
        删除会话
        
        DELETE /api/sessions/{index}
        """
        self._backend.delete_session(index)
        return {"status": "ok"}
    
    def get_agents(self) -> List[Dict]:
        """
        获取智能体列表
        
        GET /api/agents
        """
        agents = self._backend.get_primary_agents()
        return [
            {
                "name": a.name,
                "description": a.description,
                "mode": a.mode
            }
            for a in agents
        ]
    
    def get_current_agent(self) -> str:
        """
        获取当前智能体
        
        GET /api/agents/current
        """
        return self._backend.get_current_agent()
    
    def switch_agent(self, agent_name: str) -> Dict:
        """
        切换智能体
        
        POST /api/agents/switch
        Body: { "agent_name": "build" }
        """
        self._backend.switch_agent(agent_name)
        return {"status": "ok"}
    
    def approve_permission(self, tool_call_id: str, auto_allow: bool = False) -> Dict:
        """
        批准权限
        
        POST /api/permissions/approve
        Body: { "tool_call_id": "...", "auto_allow": false }
        """
        self._backend.approve_permission(tool_call_id, auto_allow=auto_allow)
        return {"status": "ok"}
    
    def deny_permission(self, tool_call_id: str) -> Dict:
        """
        拒绝权限
        
        POST /api/permissions/deny
        Body: { "tool_call_id": "..." }
        """
        self._backend.deny_permission(tool_call_id)
        return {"status": "ok"}
    
    def get_context_usage(self) -> Dict:
        """
        获取上下文使用情况
        
        GET /api/context/usage
        """
        token_count, limit = self._backend.get_context_usage()
        return {
            "token_count": token_count,
            "limit": limit,
            "percentage": (token_count / limit * 100) if limit > 0 else 0
        }
    
    # ========== WebSocket ==========
    
    @property
    def ws_manager(self) -> WebSocketManager:
        """获取 WebSocket 管理器"""
        return self._ws_manager


# ========== Flask 集成 ==========
def create_flask_app(backend, host: str = "0.0.0.0", port: int = 8080):
    """
    创建 Flask 应用
    
    Args:
        backend: ChatBackend 实例
        host: 监听地址
        port: 监听端口
    """
    try:
        from flask import Flask, jsonify, request
        from flask_sock import Sock
    except ImportError:
        logger.error("[BackendAPI] 需要安装 flask, flask-sock")
        return None
    
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'dri fox secret'
    sock = Sock(app)
    
    api = BackendAPI(backend)
    
    # REST API 路由
    @app.route("/api/chat/send", methods=["POST"])
    def chat_send():
        data = request.json
        return jsonify(api.send_message(data.get("text", ""), data.get("agent_name")))
    
    @app.route("/api/chat/stop", methods=["POST"])
    def chat_stop():
        return jsonify(api.stop_streaming())
    
    @app.route("/api/sessions", methods=["GET"])
    def sessions_list():
        return jsonify([asdict(s) for s in api.get_sessions()])
    
    @app.route("/api/sessions", methods=["POST"])
    def sessions_create():
        return jsonify(asdict(api.create_session()))
    
    @app.route("/api/sessions/current", methods=["GET"])
    def sessions_current():
        s = api.get_current_session()
        return jsonify(asdict(s) if s else None)
    
    @app.route("/api/sessions/<int:index>", methods=["DELETE"])
    def sessions_delete(index):
        return jsonify(api.delete_session(index))
    
    @app.route("/api/sessions/switch", methods=["POST"])
    def sessions_switch():
        data = request.json
        return jsonify(api.switch_session(data.get("index", 0)))
    
    @app.route("/api/agents", methods=["GET"])
    def agents_list():
        return jsonify(api.get_agents())
    
    @app.route("/api/agents/current", methods=["GET"])
    def agents_current():
        return jsonify(api.get_current_agent())
    
    @app.route("/api/agents/switch", methods=["POST"])
    def agents_switch():
        data = request.json
        return jsonify(api.switch_agent(data.get("agent_name", "")))
    
    @app.route("/api/context/usage", methods=["GET"])
    def context_usage():
        return jsonify(api.get_context_usage())
    
    # WebSocket 路由
    @sock.route("/ws")
    def websocket_handler(ws):
        conn_id = api.ws_manager.add_connection(ws)
        try:
            while True:
                # 接收前端消息
                data = ws.receive()
                if data:
                    try:
                        msg = json.loads(data)
                        if msg.get("type") == "send_message":
                            api.send_message(msg.get("text", ""), msg.get("agent_name"))
                        elif msg.get("type") == "stop":
                            api.stop_streaming()
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.error(f"[WebSocket] 连接错误: {e}")
        finally:
            api.ws_manager.remove_connection(conn_id)
    
    logger.info(f"[BackendAPI] Flask 服务启动: http://{host}:{port}")
    return app


# ========== FastAPI 集成 ==========
def create_fastapi_app(backend, host: str = "0.0.0.0", port: int = 8080):
    """
    创建 FastAPI 应用
    
    Args:
        backend: ChatBackend 实例
        host: 监听地址
        port: 监听端口
    """
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        import uvicorn
    except ImportError:
        logger.error("[BackendAPI] 需要安装 fastapi, uvicorn")
        return None
    
    app = FastAPI(title="Drifox Backend API")
    
    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    api = BackendAPI(backend)
    
    # REST API 路由
    @app.post("/api/chat/send")
    async def chat_send(data: dict):
        return api.send_message(data.get("text", ""), data.get("agent_name"))
    
    @app.post("/api/chat/stop")
    async def chat_stop():
        return api.stop_streaming()
    
    @app.get("/api/sessions")
    async def sessions_list():
        return [asdict(s) for s in api.get_sessions()]
    
    @app.post("/api/sessions")
    async def sessions_create():
        return asdict(api.create_session())
    
    @app.get("/api/sessions/current")
    async def sessions_current():
        s = api.get_current_session()
        return asdict(s) if s else None
    
    @app.delete("/api/sessions/{index}")
    async def sessions_delete(index: int):
        return api.delete_session(index)
    
    @app.post("/api/sessions/switch")
    async def sessions_switch(data: dict):
        return api.switch_session(data.get("index", 0))
    
    @app.get("/api/agents")
    async def agents_list():
        return api.get_agents()
    
    @app.get("/api/agents/current")
    async def agents_current():
        return api.get_current_agent()
    
    @app.post("/api/agents/switch")
    async def agents_switch(data: dict):
        return api.switch_agent(data.get("agent_name", ""))
    
    @app.get("/api/context/usage")
    async def context_usage():
        return api.get_context_usage()
    
    # WebSocket
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        conn_id = api.ws_manager.add_connection(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "send_message":
                        api.send_message(msg.get("text", ""), msg.get("agent_name"))
                    elif msg.get("type") == "stop":
                        api.stop_streaming()
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"[WebSocket] 连接错误: {e}")
        finally:
            api.ws_manager.remove_connection(conn_id)
    
    logger.info(f"[BackendAPI] FastAPI 服务启动: http://{host}:{port}")
    return app


def create_api_server(backend, host: str = "0.0.0.0", port: int = 8080, framework: str = "auto"):
    """
    创建 API 服务器（自动选择可用的框架）
    
    Args:
        backend: ChatBackend 实例
        host: 监听地址
        port: 监听端口
        framework: "flask", "fastapi", "auto"
    """
    if framework == "auto":
        # 尝试 FastAPI
        app = create_fastapi_app(backend, host, port)
        if app:
            return app
        # 回退到 Flask
        app = create_flask_app(backend, host, port)
        if app:
            return app
        logger.error("[BackendAPI] 没有可用的 Web 框架（需要 fastapi 或 flask）")
        return None
    
    if framework == "fastapi":
        return create_fastapi_app(backend, host, port)
    elif framework == "flask":
        return create_flask_app(backend, host, port)
    else:
        logger.error(f"[BackendAPI] 不支持的框架: {framework}")
        return None


# ========== 便捷函数 ==========
def start_api_server(backend, host: str = "0.0.0.0", port: int = 8080, framework: str = "auto"):
    """
    启动 API 服务器
    
    Args:
        backend: ChatBackend 实例
        host: 监听地址
        port: 监听端口
        framework: "flask", "fastapi", "auto"
    """
    app = create_api_server(backend, host, port, framework)
    if app is None:
        return
    
    try:
        import uvicorn
        uvicorn.run(app, host=host, port=port)
    except ImportError:
        try:
            from flask import Flask
            if isinstance(app, Flask):
                app.run(host=host, port=port, debug=False, threaded=True)
        except Exception as e:
            logger.error(f"[BackendAPI] 启动失败: {e}")
