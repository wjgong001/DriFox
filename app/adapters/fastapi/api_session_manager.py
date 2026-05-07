# -*- coding: utf-8 -*-
"""
独立的 API 会话处理器 - 使用 Core 层，不依赖 UI
"""

import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from loguru import logger

from app.core.chat_session import SessionManager, ChatSession
from app.core.store.session_store import SessionStore

class StandaloneAPISessionManager:
    """独立的 API 会话管理器 - 不依赖 UI"""
    
    def __init__(self, canvas_id: str = "api"):
        self._canvas_id = canvas_id
        self._session_manager = SessionManager()
        self._session_store = SessionStore(db_dir=".drifox")
        
        # 加载已有会话
        self._load_sessions()
    
    def _load_sessions(self):
        """从存储加载会话"""
        try:
            sessions = self._session_store.list_sessions(self._canvas_id)
            for session_data in sessions:
                session = self._session_to_object(session_data)
                if session:
                    self._session_manager.sessions.append(session)
            logger.info(f"[StandaloneAPI] 加载了 {len(sessions)} 个会话")
        except Exception as e:
            logger.error(f"[StandaloneAPI] 加载会话失败: {e}")
    
    def _session_to_object(self, data: Dict) -> Optional[ChatSession]:
        """将数据转换为 ChatSession 对象"""
        try:
            return ChatSession.from_dict({
                "session_id": data.get("session_id", str(uuid.uuid4())),
                "name": data.get("title", "未命名"),
                "messages": data.get("messages", []),
                "topic_summary": data.get("topic_summary", ""),
                "created_at": data.get("created_at", datetime.now().isoformat()),
                "last_updated": data.get("last_updated", datetime.now().isoformat()),
            })
        except Exception as e:
            logger.error(f"[StandaloneAPI] 转换会话失败: {e}")
            return None
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        """列出所有会话"""
        sessions = []
        for session in self._session_manager.sessions:
            sessions.append({
                "session_id": session.session_id,
                "title": session.name,
                "created_at": session.created_at,
                "last_updated": session.last_updated,
                "message_count": len(session.messages),
            })
        return sessions
    
    def create_session(self, title: str = "") -> Optional[Dict[str, Any]]:
        """创建新会话"""
        try:
            session = self._session_manager.create_new_session()
            if title:
                session.name = title
            
            # 保存到存储（传入 canvas_id 合并到会话数据中）
            session_dict = session.to_dict()
            session_dict["canvas_id"] = self._canvas_id
            self._session_store.save_session(session_dict)
            
            return {
                "session_id": session.session_id,
                "title": session.name,
                "created_at": session.created_at,
                "message_count": 0,
            }
        except Exception as e:
            logger.error(f"[StandaloneAPI] 创建会话失败: {e}")
            return None
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话详情"""
        for session in self._session_manager._sessions:
            if session.session_id == session_id:
                return {
                    "session_id": session.session_id,
                    "title": session.name,
                    "created_at": session.created_at.isoformat() if session.created_at else "",
                    "last_updated": session.last_updated.isoformat() if session.last_updated else "",
                    "message_count": len(session.messages),
                }
        return None
    
    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        for i, session in enumerate(self._session_manager.sessions):
            if session.session_id == session_id:
                self._session_manager.sessions.pop(i)
                # 从存储删除（使用 session_id 标识）
                self._session_store.delete_session(session_id)
                return True
        return False


# 全局单例
_api_session_manager: Optional[StandaloneAPISessionManager] = None

def get_standalone_api_manager() -> StandaloneAPISessionManager:
    """获取独立的 API 会话管理器"""
    global _api_session_manager
    if _api_session_manager is None:
        _api_session_manager = StandaloneAPISessionManager()
    return _api_session_manager