# -*- coding: utf-8 -*-
"""Session management routes."""

from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    title: Optional[str] = ""


def create_session_router(
    list_sessions: callable,
    get_session: callable,
    create_session: callable,
    delete_session: callable,
) -> APIRouter:
    """创建会话管理路由（依赖注入版本）
    
    Args:
        list_sessions: 列出所有会话的回调
        get_session: 获取会话详情的回调
        create_session: 创建会话的回调
        delete_session: 删除会话的回调
    
    Returns:
        APIRouter: 配置好的路由
    """
    
    @router.get("", response_model=Dict[str, Any])
    async def list_all_sessions():
        """列出所有会话
        
        返回所有会话列表，按最后更新时间倒序排列。
        """
        sessions = list_sessions()
        return {"success": True, "sessions": sessions}
    
    @router.post("", response_model=Dict[str, Any])
    async def create_new_session(request: Optional[CreateSessionRequest] = None):
        """创建新会话
        
        Args:
            request: 可选的请求体，包含 title 字段
        
        Returns:
            新创建的会话信息
        """
        title = request.title if request else ""
        session = create_session(title=title)
        
        if not session:
            raise HTTPException(status_code=500, detail="创建会话失败")
        
        return {"success": True, "session": session}
    
    @router.get("/{session_id}", response_model=Dict[str, Any])
    async def get_session_detail(session_id: str):
        """获取会话详情
        
        Args:
            session_id: 会话 ID
        
        Returns:
            会话详细信息
        """
        session = get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=404,
                detail=f"会话 {session_id} 不存在"
            )
        
        return {"success": True, "session": session}
    
    @router.delete("/{session_id}", response_model=Dict[str, Any])
    async def delete_session_by_id(session_id: str):
        """删除会话
        
        Args:
            session_id: 要删除的会话 ID
        
        Returns:
            删除结果
        """
        if delete_session(session_id):
            return {"success": True, "message": f"会话 {session_id} 已删除"}
        else:
            raise HTTPException(status_code=500, detail="删除会话失败")
    
    return router
