# -*- coding: utf-8 -*-
"""
TaskWatcher 数据库模块
使用项目统一的 sessions.db 数据库
"""

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional
from loguru import logger

# 项目本地的 .drifox 目录
DRIFOX_DIR = ".drifox"
DB_FILENAME = "sessions.db"


class TaskWatcherDB:
    """TaskWatcher 数据库访问类
    
    使用项目统一的 sessions.db 数据库，在其中创建 task_watcher 专用表
    """
    
    _instance: Optional["TaskWatcherDB"] = None
    _lock = threading.Lock()
    
    def __init__(self, db_path: Optional[str] = None):
        """初始化数据库
        
        Args:
            db_path: 可选的数据库路径，默认使用 sessions.db
        """
        if db_path:
            self._db_path = db_path
        else:
            self._db_dir = DRIFOX_DIR
            self._db_path = os.path.join(self._db_dir, DB_FILENAME)
        self._initialized = False
        self._local = threading.local()
    
    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "TaskWatcherDB":
        """获取单例实例
        
        Args:
            db_path: 可选，指定数据库路径（仅在第一次调用时有效）
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance
    
    @classmethod
    def reset_instance(cls):
        """重置单例实例（用于测试）"""
        if cls._instance:
            cls._instance = None
    
    @property
    def db_path(self) -> str:
        """获取数据库路径"""
        return self._db_path
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self.ensure_initialized()
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn
    
    def rollback(self):
        """回滚事务"""
        try:
            conn = self._get_connection()
            conn.rollback()
        except Exception:
            pass
    
    def ensure_initialized(self) -> bool:
        """确保数据库已初始化"""
        if self._initialized:
            return True
        
        # 确保目录存在
        os.makedirs(self._db_dir if hasattr(self, '_db_dir') else DRIFOX_DIR, exist_ok=True)
        
        # 确保 sessions.db 存在并已初始化表
        return self._init_schema()
    
    def _init_schema(self) -> bool:
        """初始化数据库表结构"""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # task_configs 表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_configs (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    config_json TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            
            # task_queue 表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    execute_at TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    retry_count INTEGER DEFAULT 0
                )
            """)
            
            # task_execution_logs 表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    session_id TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    status TEXT,
                    result_summary TEXT,
                    output_file TEXT
                )
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_queue_status 
                ON task_queue(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_queue_priority 
                ON task_queue(priority DESC)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_execution_task_id 
                ON task_execution_logs(task_id)
            """)
            
            conn.commit()
            conn.close()
            
            self._initialized = True
            logger.info(f"[TaskWatcherDB] 初始化完成: {self._db_path}")
            return True
            
        except Exception as e:
            logger.error(f"[TaskWatcherDB] 初始化失败: {e}")
            return False
    
    def execute(self, sql: str, params: tuple = ()):
        """执行 SQL 语句"""
        self.ensure_initialized()
        conn = self._get_connection()
        try:
            return conn.execute(sql, params)
        except Exception as e:
            conn.rollback()
            raise e
    
    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """查询单条记录"""
        cursor = self.execute(sql, params)
        return cursor.fetchone()
    
    def fetch_all(self, sql: str, params: tuple = ()) -> list:
        """查询所有记录"""
        cursor = self.execute(sql, params)
        return cursor.fetchall()
    
    def commit(self):
        """提交事务"""
        conn = self._get_connection()
        conn.commit()
    
    def close(self):
        """关闭连接"""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# 保持向后兼容的别名
Database = TaskWatcherDB