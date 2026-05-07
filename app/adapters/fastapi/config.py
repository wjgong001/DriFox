# -*- coding: utf-8 -*-
"""
FastAPI 配置
使用 Pydantic Settings 管理配置
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """FastAPI 应用配置"""
    
    # 服务配置
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    
    # CORS 配置
    cors_origins: list[str] = ["*"]
    cors_allow_credentials: bool = True
    cors_allow_methods: list[str] = ["*"]
    cors_allow_headers: list[str] = ["*"]
    
    # 日志配置
    log_level: str = "INFO"
    
    # API 前缀
    api_prefix: str = "/api/v1"
    
    # LLM 配置 (用于代理请求)
    llm_api_base: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: str = "qwen/qwen3-30b-a3b"
    llm_max_tokens: int = 2048
    llm_temperature: float = 0.7

    class Config:
        env_prefix = "FASTAPI_"
        case_sensitive = False


# 全局配置实例
settings = Settings()
