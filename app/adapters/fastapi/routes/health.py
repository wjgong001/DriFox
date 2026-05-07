# -*- coding: utf-8 -*-
"""Health check routes."""

from typing import Dict, Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/health", tags=["health"])


def create_health_router(
    get_running: callable,
    get_service_info: callable,
) -> APIRouter:
    """创建健康检查路由（依赖注入版本）
    
    Args:
        get_running: 获取服务是否运行的回调
        get_service_info: 获取服务信息的回调
    
    Returns:
        APIRouter: 配置好的路由
    """
    
    @router.get("", response_model=Dict[str, Any])
    async def health_check():
        """健康检查
        
        返回服务健康状态，包括：
        - status: 服务状态 (ok/stopped)
        - service: 服务名称
        - version: 服务版本
        - running: 是否正在运行
        - address: 服务地址（如果运行中）
        - ui_linked: 是否与 UI 链接
        """
        running = get_running()
        service_info = get_service_info()
        
        return {
            "status": "ok" if running else "stopped",
            "service": service_info.get("name", "llm_chatter_api"),
            "version": service_info.get("version", "2.0.0"),
            "running": running,
            "address": service_info.get("address"),
            "ui_linked": service_info.get("ui_linked", False),
        }
    
    return router
