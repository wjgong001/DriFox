# -*- coding: utf-8 -*-
"""
FastAPI 请求/响应模型
使用 Pydantic 定义 API 数据结构
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class MessageContent(BaseModel):
    """消息内容"""
    role: str = Field(..., description="角色: user, assistant, system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    """聊天请求"""
    messages: List[MessageContent] = Field(..., description="消息列表")
    model: Optional[str] = Field(None, description="模型名称")
    temperature: Optional[float] = Field(None, ge=0, le=2, description="温度参数")
    max_tokens: Optional[int] = Field(None, ge=1, description="最大 token 数")
    stream: bool = Field(False, description="是否流式输出")
    session_id: Optional[str] = Field(None, description="会话 ID")
    
    class Config:
        json_schema_extra = {
            "example": {
                "messages": [
                    {"role": "user", "content": "你好"}
                ],
                "model": "qwen/qwen3-30b-a3b",
                "stream": False
            }
        }


class ChatResponse(BaseModel):
    """聊天响应"""
    id: str = Field(..., description="响应 ID")
    model: str = Field(..., description="模型名称")
    content: str = Field(..., description="响应内容")
    created: int = Field(..., description="创建时间戳")
    usage: Optional[Dict[str, Any]] = Field(None, description="Token 使用量")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "chatcmpl-xxx",
                "model": "qwen/qwen3-30b-a3b",
                "content": "你好！有什么可以帮助你的吗？",
                "created": 1234567890,
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
            }
        }


class StreamChunk(BaseModel):
    """流式输出片段"""
    id: str = Field(..., description="响应 ID")
    choices: List[Dict[str, Any]] = Field(..., description="选项列表")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "chatcmpl-xxx",
                "choices": [{"delta": {"content": "你好"}}]
            }
        }


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = Field("ok", description="服务状态")
    version: str = Field("1.0.0", description="API 版本")
    models: Optional[List[str]] = Field(None, description="可用模型列表")
