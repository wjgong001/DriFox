# -*- coding: utf-8 -*-
"""FastAPI routes package.

导出所有路由工厂函数，供 api_server.py 使用。
"""

from app.adapters.fastapi.routes.health import create_health_router
from app.adapters.fastapi.routes.session import create_session_router
from app.adapters.fastapi.routes.chat import create_chat_router, router as chat_router

__all__ = [
    "create_health_router",
    "create_session_router",
    "create_chat_router",
    "chat_router",
]
