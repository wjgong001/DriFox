# -*- coding: utf-8 -*-
"""
FastAPI 适配器模块
提供 FastAPI 应用创建和路由注册
"""

from app.adapters.fastapi.server import create_app, app

__all__ = ["create_app", "app"]
