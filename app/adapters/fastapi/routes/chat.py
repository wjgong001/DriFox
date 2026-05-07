# -*- coding: utf-8 -*-
"""
聊天流式 API 路由

提供独立的聊天流式端点，与 SessionManager 解耦。
支持并发调用，每个请求创建独立的 ChatEngine 实例。

端点：
- POST /chat/stream - SSE 流式聊天端点
- POST /chat/stop   - 停止流式输出
"""

import asyncio
import json
import threading
import uuid
from typing import Optional, Dict, Any, AsyncGenerator, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

# ==================== 全局流上下文管理 ====================

class StreamContext:
    """流式请求上下文（线程安全）"""

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self.engine = None
        self.session_id = ""
        self.buffer: Dict[str, Any] = {
            "content": "",
            "started": False,
            "finished": False,
        }
        self._active = True
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._pending_event: Optional[Dict[str, Any]] = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = active
        if not active:
            self._event.set()

    def wait_for_event(self, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
        """等待事件发生"""
        self._event.wait(timeout=timeout)
        self._event.clear()
        with self._lock:
            event = self._pending_event
            self._pending_event = None
            return event

    def push_event(self, event_data: Dict[str, Any]) -> None:
        """推送事件"""
        with self._lock:
            self._pending_event = event_data
        self._event.set()

    def append_content(self, piece: str) -> None:
        with self._lock:
            self.buffer["content"] += piece

    def get_content(self) -> str:
        with self._lock:
            return self.buffer.get("content", "")


# 全局活跃流管理器
_active_streams: Dict[str, StreamContext] = {}
_streams_lock = threading.Lock()


def _get_session_handler():
    """获取会话处理器"""
    from app.api.api_server import LLMAPIService
    return LLMAPIService.get_session_handler()


def _register_stream(stream_id: str, ctx: StreamContext) -> None:
    """注册活跃流"""
    with _streams_lock:
        _active_streams[stream_id] = ctx


def _unregister_stream(stream_id: str) -> None:
    """注销流"""
    with _streams_lock:
        _active_streams.pop(stream_id, None)


def _get_stream(stream_id: str) -> Optional[StreamContext]:
    """获取流上下文"""
    with _streams_lock:
        return _active_streams.get(stream_id)


def _handle_engine_event(stream_id: str, event_name: str, *args, **kwargs) -> None:
    """处理引擎事件"""
    ctx = _get_stream(stream_id)
    if not ctx or not ctx.is_active:
        return

    event_data = {"stream_id": stream_id, "event": event_name}

    if event_name == "content" and args:
        piece = args[0] if args else ""
        ctx.append_content(piece)
        event_data["data"] = {"piece": piece}

    elif event_name == "reasoning" and args:
        reasoning = args[0] if args else ""
        event_data["data"] = {"reasoning": reasoning}

    elif event_name == "tool_call_started":
        tool_call_id = args[0] if len(args) > 0 else ""
        tool_name = args[1] if len(args) > 1 else ""
        arguments = args[2] if len(args) > 2 else {}
        event_data["data"] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }

    elif event_name == "tool_result":
        tool_call_id = args[0] if len(args) > 0 else ""
        tool_name = args[1] if len(args) > 1 else ""
        result = args[2] if len(args) > 2 else ""
        result_str = result if isinstance(result, str) else str(result)
        event_data["data"] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result_str,
        }

    elif event_name == "stream_started":
        ctx.buffer["started"] = True
        event_data["data"] = {}

    elif event_name == "stream_finished":
        ctx.buffer["finished"] = True
        final_content = ctx.get_content()
        event_data["data"] = {"content": final_content, "finished": True}
        ctx.set_active(False)

    elif event_name == "error":
        error_msg = args[0] if args else str(kwargs.get("error", "Unknown error"))
        event_data["data"] = {"error": error_msg}
        ctx.set_active(False)

    elif event_name == "permission":
        # API 端自动允许权限
        tool_call_id = args[0] if len(args) > 0 else ""
        if ctx.engine:
            ctx.engine.approve_tool_permission(tool_call_id, True)
        return

    elif event_name == "messages_updated":
        # 更新引擎的 session.messages
        messages = args[0] if args else []
        if ctx.engine:
            session = ctx.engine._session_manager.get_current_session()
            if session:
                session.set_messages(messages, preserve_compaction=True)
        return  # 不推送到 SSE

    # 推送事件
    ctx.push_event(event_data)


# ==================== 路由工厂函数 ====================

def create_chat_router(prefix: str = "/chat") -> APIRouter:
    """
    创建聊天路由实例（可自定义前缀）

    Args:
        prefix: 路由前缀，默认 "/chat"

    Returns:
        APIRouter 实例
    """
    router = APIRouter(prefix=prefix, tags=["chat"])

    @router.post("/stream")
    async def chat_stream(request: Dict[str, Any]) -> StreamingResponse:
        """
        SSE 流式聊天端点

        请求体：
        {
            "session_id": "可选，指定会话 ID，不提供则创建新会话",
            "message": "用户消息（必填）",
            "context": {}  // 可选，上下文参数
        }

        SSE 事件：
        - started: 流开始，包含 stream_id
        - content: 收到的内容片段
        - tool_call_started: 工具调用开始
        - tool_result: 工具执行结果
        - reasoning: 思考内容（DeepSeek 等）
        - error: 错误
        - stream_finished: 流结束
        - complete: 完整响应
        """
        handler = _get_session_handler()
        if not handler:
            raise HTTPException(
                status_code=503,
                detail="会话处理器未初始化，请确保 LLMChatter 窗口已打开"
            )

        message = request.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message 是必填项")

        session_id = request.get("session_id")
        context_params = request.get("context", {})

        # 如果没有指定 session_id，创建一个新会话
        if not session_id:
            new_session = handler.create_session(title="API 对话")
            if not new_session:
                raise HTTPException(status_code=500, detail="创建会话失败")
            session_id = new_session.get("id")
            logger.info(f"[ChatStream] 创建新会话: {session_id}")

        # 生成流 ID
        stream_id = str(uuid.uuid4())

        # 从 SQLite 加载目标会话
        target_session = handler.switch_session(session_id)
        if not target_session:
            raise HTTPException(
                status_code=404,
                detail=f"会话 {session_id} 不存在"
            )

        # 创建流上下文
        ctx = StreamContext(stream_id)
        ctx.session_id = session_id
        _register_stream(stream_id, ctx)

        # 构建 worker 回调（API 模式直接调用）
        def make_callback(event_name: str):
            def callback(*args, **kwargs):
                _handle_engine_event(stream_id, event_name, *args, **kwargs)
            return callback

        worker_callbacks = {
            "content_received": make_callback("content"),
            "reasoning_content_received": make_callback("reasoning"),
            "tool_call_started": make_callback("tool_call_started"),
            "tool_result_received": make_callback("tool_result"),
            "error_occurred": make_callback("error"),
            "finished_with_content": make_callback("stream_finished"),
            "finished_with_messages": make_callback("messages_updated"),
            "question_asked": make_callback("question"),
            "permission_approval_requested": make_callback("permission"),
        }

        async def event_generator() -> AsyncGenerator[str, None]:
            try:
                # 发送开始事件
                yield f"data: {json.dumps({'stream_id': stream_id, 'event': 'started', 'session_id': session_id})}\n\n"

                # 创建独立的 ChatEngine
                engine = handler._create_isolated_chat_engine(
                    worker_callbacks=worker_callbacks,
                    api_mode=True,
                    target_session=target_session,
                    context_id=stream_id,
                )
                ctx.engine = engine

                # 在线程中执行对话
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: engine.send_message(message, context_params)
                )

                # 等待并推送事件
                while ctx.is_active:
                    event = ctx.wait_for_event(timeout=0.5)
                    if event:
                        yield f"data: {json.dumps(event)}\n\n"
                        if event.get("event") == "stream_finished":
                            break
                        elif event.get("event") == "error":
                            break
                    else:
                        # 超时，检查引擎状态
                        if engine and not engine._is_streaming:
                            break

                # 持久化会话
                handler._persist_current_session_from_engine(engine)

                # 发送完成事件
                final_content = ctx.get_content()
                yield f"data: {json.dumps({'event': 'complete', 'content': final_content, 'stream_id': stream_id})}\n\n"

            except Exception as e:
                logger.exception(f"[ChatStream] 错误: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            finally:
                # 清理
                _unregister_stream(stream_id)
                if ctx:
                    ctx.set_active(False)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/stop")
    async def stop_chat(request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        停止流式输出

        请求体（可选）：
        {
            "stream_id": "指定要停止的流 ID，不提供则停止所有活跃流"
        }

        返回：
        {
            "success": true,
            "message": "已停止流式请求",
            "stream_id": "被停止的流 ID"
        }
        """
        handler = _get_session_handler()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")

        stream_id = request.get("stream_id") if request else None
        target_id = stream_id

        if not target_id:
            # 停止第一个活跃流
            with _streams_lock:
                active_ids = [sid for sid, ctx in _active_streams.items() if ctx.is_active]
                target_id = active_ids[0] if active_ids else None

        if not target_id:
            return {"success": False, "message": "没有活跃的流请求"}

        ctx = _get_stream(target_id)
        if not ctx:
            return {"success": False, "message": f"流 {target_id} 不存在"}

        ctx.set_active(False)

        if ctx.engine:
            ctx.engine.stop()

        # 持久化
        if ctx.engine:
            handler._persist_current_session_from_engine(ctx.engine)

        _unregister_stream(target_id)
        logger.info(f"[ChatStream] 已停止流请求: {target_id}")

        return {
            "success": True,
            "message": "已停止流式请求",
            "stream_id": target_id
        }

    return router


# 导出默认路由实例
router = create_chat_router()
