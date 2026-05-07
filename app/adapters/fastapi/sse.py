# -*- coding: utf-8 -*-
"""
SSE (Server-Sent Events) 流式响应支持

将流式 chunks 转换为 SSE 格式，并提供 EventSourceResponse 创建函数。
"""

from typing import AsyncIterator, Dict, Any, Optional
import json
import asyncio

from sse_starlette import EventSourceResponse


async def sse_generator(
    chunks: AsyncIterator[str],
    event: str = "message",
    data_key: str = "data",
    retry: int = 1500,
) -> AsyncIterator[Dict[str, Any]]:
    """
    将流式 chunks 转换为 SSE 格式事件。

    Args:
        chunks: 异步流式数据源（字符串 chunks）
        event: 事件类型，默认为 "message"
        data_key: 数据字段名，默认为 "data"
        retry: 重连间隔（毫秒）

    Yields:
        SSE 格式的事件字典
    """
    # 先发送一次空消息保持连接
    yield {
        "event": event,
        "data": "",
        "retry": retry,
    }

    async for chunk in chunks:
        if chunk:
            yield {
                "event": event,
                "data": chunk,
                "retry": retry,
            }


async def sse_json_generator(
    chunks: AsyncIterator[str],
    event: str = "message",
    retry: int = 1500,
) -> AsyncIterator[Dict[str, Any]]:
    """
    将流式 chunks 转换为 SSE JSON 格式事件。

    适用于发送结构化 JSON 数据的 SSE 流。

    Args:
        chunks: 异步流式数据源
        event: 事件类型
        retry: 重连间隔（毫秒）

    Yields:
        包含 JSON 序列化后数据的 SSE 事件
    """
    async for chunk in chunks:
        if chunk:
            try:
                json_data = json.loads(chunk)
                yield {
                    "event": event,
                    "data": json.dumps(json_data),
                    "retry": retry,
                }
            except json.JSONDecodeError:
                # 非 JSON 数据，直接发送原始字符串
                yield {
                    "event": event,
                    "data": chunk,
                    "retry": retry,
                }


def create_sse_response(
    chunks: AsyncIterator[str],
    event: str = "message",
    media_type: str = "text/event-stream",
    headers: Optional[Dict[str, str]] = None,
) -> EventSourceResponse:
    """
    创建 SSE EventSourceResponse。

    Args:
        chunks: 异步流式数据源
        event: 事件类型
        media_type: 媒体类型，默认为 "text/event-stream"
        headers: 额外的响应头

    Returns:
        EventSourceResponse 实例
    """
    generator = sse_generator(chunks, event=event)
    
    extra_headers = headers or {}
    extra_headers.update({
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
    })
    
    return EventSourceResponse(
        generator,
        media_type=media_type,
        headers=extra_headers,
    )


def create_json_sse_response(
    chunks: AsyncIterator[str],
    event: str = "message",
    media_type: str = "text/event-stream",
    headers: Optional[Dict[str, str]] = None,
) -> EventSourceResponse:
    """
    创建 SSE EventSourceResponse（JSON 格式）。

    适用于发送结构化 JSON 数据的 SSE 流。

    Args:
        chunks: 异步流式数据源
        event: 事件类型
        media_type: 媒体类型
        headers: 额外的响应头

    Returns:
        EventSourceResponse 实例
    """
    generator = sse_json_generator(chunks, event=event)
    
    extra_headers = headers or {}
    extra_headers.update({
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    
    return EventSourceResponse(
        generator,
        media_type=media_type,
        headers=extra_headers,
    )
