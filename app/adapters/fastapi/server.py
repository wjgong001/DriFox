# -*- coding: utf-8 -*-
"""
FastAPI 主服务器入口

提供：
- create_app() - 创建 FastAPI 应用
- 配置 CORS 中间件
- 注册所有路由
- 主函数入口
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger

# 全局应用实例
app: Optional[FastAPI] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("[FastAPI] 应用启动")
    yield
    logger.info("[FastAPI] 应用关闭")


def create_app() -> FastAPI:
    """
    创建 FastAPI 应用实例
    
    配置：
    - CORS 中间件（允许所有来源）
    - 健康检查端点
    - API 路由注册
    
    Returns:
        FastAPI: 配置好的 FastAPI 应用实例
    """
    global app
    
    if app is not None:
        return app
    
    app = FastAPI(
        title="DriFoxx API",
        description="DriFoxx 对话系统的 FastAPI 接口",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    # ==================== CORS 配置 ====================
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 生产环境建议限制具体域名
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # ==================== 健康检查 ====================
    @app.get("/health", tags=["健康检查"])
    async def health_check() -> Dict[str, Any]:
        """健康检查端点"""
        return {
            "status": "ok",
            "service": "drifox_api",
            "version": "1.0.0",
        }
    
    # ==================== 注册路由 ====================
    _register_routes(app)
    
    return app


def _register_routes(app: FastAPI) -> None:
    """
    注册所有路由
    
    目前注册的路由：
    - /sessions/* - 会话管理
    - /chat/* - 对话接口
    
    未来可扩展：
    - /agents/* - Agent 管理
    - /tools/* - 工具管理
    """
    # 延迟导入避免循环依赖
    try:
        from app.api.api_session_handler import get_session_handler
        _register_session_routes(app, get_session_handler)
        _register_chat_routes(app, get_session_handler)
        logger.info("[FastAPI] 路由注册完成")
    except ImportError as e:
        logger.warning(f"[FastAPI] 路由注册失败: {e}")


def _register_session_routes(app: FastAPI, get_handler_func) -> None:
    """注册会话管理路由"""
    
    @app.get("/sessions", tags=["会话管理"])
    async def list_sessions() -> Dict[str, Any]:
        """获取所有会话列表"""
        handler = get_handler_func()
        if not handler:
            raise HTTPException(
                status_code=503,
                detail="会话处理器未初始化"
            )
        sessions = handler.list_sessions()
        return {"success": True, "sessions": sessions}

    @app.post("/sessions", tags=["会话管理"])
    async def create_session(request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """创建新会话"""
        handler = get_handler_func()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")
        
        title = request.get("title", "") if request else ""
        session = handler.create_session(title=title)
        if not session:
            raise HTTPException(status_code=500, detail="创建会话失败")
        
        return {"success": True, "session": session}

    @app.get("/sessions/{session_id}", tags=["会话管理"])
    async def get_session(session_id: str) -> Dict[str, Any]:
        """获取指定会话详情"""
        handler = get_handler_func()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")
        
        session = handler.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
        
        return {"success": True, "session": session}

    @app.delete("/sessions/{session_id}", tags=["会话管理"])
    async def delete_session(session_id: str) -> Dict[str, Any]:
        """删除会话"""
        handler = get_handler_func()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")
        
        if handler.delete_session(session_id):
            return {"success": True, "message": f"会话 {session_id} 已删除"}
        else:
            raise HTTPException(status_code=500, detail="删除会话失败")


def _register_chat_routes(app: FastAPI, get_handler_func) -> None:
    """注册对话路由"""
    
    @app.post("/sessions/{session_id}/chat/stream", tags=["对话"])
    async def chat_stream(
        session_id: str,
        request: Dict[str, Any]
    ) -> StreamingResponse:
        """
        在指定会话中对话（流式 SSE）
        
        Request Body:
        {
            "message": "用户消息",
            "context": {}  // 可选，上下文参数
        }
        """
        import json
        
        handler = get_handler_func()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")

        message = request.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message 是必填项")

        context_params = request.get("context", {})

        async def event_generator():
            try:
                async for event in handler.chat_stream(
                    session_id=session_id,
                    message=message,
                    context_params=context_params,
                ):
                    yield event
            except Exception as e:
                logger.exception(f"[FastAPI] chat_stream 错误: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/chat/stop", tags=["对话"])
    async def stop_chat(request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """停止当前流式请求"""
        handler = get_handler_func()
        if not handler:
            raise HTTPException(status_code=503, detail="会话处理器未初始化")
        
        stream_id = request.get("stream_id") if request else None
        handler.stop_stream(stream_id)
        
        return {"success": True, "message": "已停止流式请求"}


def run_server(host: str = "0.0.0.0", port: int = 8765, **kwargs) -> None:
    """
    运行 FastAPI 服务器
    
    Args:
        host: 监听地址
        port: 监听端口
        **kwargs: 传递给 uvicorn 的其他参数
    """
    import uvicorn
    
    application = create_app()
    
    uvicorn.run(
        application,
        host=host,
        port=port,
        **kwargs
    )


if __name__ == "__main__":
    run_server()
