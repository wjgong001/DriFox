# -*- coding: utf-8 -*-
"""
ChatEngine 调度器
管理多个 ChatEngine 实例，自动分配空闲引擎执行任务
"""

from typing import Dict, List, Optional, Callable, Any, Tuple
from loguru import logger


class EngineInfo:
    """引擎信息"""
    
    def __init__(
        self,
        engine_id: str,
        chat_engine: Any,
        session_manager: Any,
        project: str = "默认项目"
    ):
        self.engine_id = engine_id
        self.chat_engine = chat_engine
        self.session_manager = session_manager
        self.project = project
        self.is_busy = False
        self.current_task_id: Optional[str] = None
        self.last_used: float = 0

    def mark_busy(self, task_id: str) -> None:
        self.is_busy = True
        self.current_task_id = task_id

    def mark_free(self) -> None:
        self.is_busy = False
        self.current_task_id = None
        import time
        self.last_used = time.time()

    @property
    def is_available(self) -> bool:
        return not self.is_busy


class EngineScheduler:
    """ChatEngine 调度器
    
    管理多个 ChatEngine 实例，自动分配空闲引擎
    支持按 Project 隔离任务会话
    """

    def __init__(self):
        self._engines: Dict[str, EngineInfo] = {}  # engine_id -> EngineInfo
        self._project_engines: Dict[str, List[str]] = {}  # project -> [engine_id, ...]
        self._default_project = "TaskWatcher"
        self._callback: Optional[Callable] = None
        
        # 默认分配策略：优先使用空闲引擎，其次使用最近未使用的
        self._strategy = "prefer_idle"
    
    def register_engine(
        self,
        engine_id: str,
        chat_engine: Any,
        session_manager: Any,
        project: str = "默认项目"
    ) -> bool:
        """注册一个 ChatEngine
        
        Args:
            engine_id: 引擎 ID（通常是窗口实例 ID）
            chat_engine: ChatEngine 实例
            session_manager: SessionManager 实例
            project: 该引擎所属的项目
            
        Returns:
            是否成功
        """
        if engine_id in self._engines:
            logger.warning(f"[EngineScheduler] 引擎已注册: {engine_id}")
            return False
        
        try:
            engine_info = EngineInfo(
                engine_id=engine_id,
                chat_engine=chat_engine,
                session_manager=session_manager,
                project=project
            )
            self._engines[engine_id] = engine_info
            
            # 按项目索引
            if project not in self._project_engines:
                self._project_engines[project] = []
            self._project_engines[project].append(engine_id)
            
            logger.info(f"[EngineScheduler] 注册引擎: {engine_id}, project={project}")
            return True
        except Exception as e:
            logger.error(f"[EngineScheduler] 注册引擎失败: {e}")
            return False

    def unregister_engine(self, engine_id: str) -> bool:
        """注销引擎
        
        Args:
            engine_id: 引擎 ID
            
        Returns:
            是否成功
        """
        if engine_id not in self._engines:
            return False
        
        engine_info = self._engines[engine_id]
        project = engine_info.project
        
        # 从项目索引中移除
        if project in self._project_engines:
            if engine_id in self._project_engines[project]:
                self._project_engines[project].remove(engine_id)
        
        # 如果引擎正在执行任务，先标记为完成
        if engine_info.is_busy:
            self.release_engine(engine_id)
        
        del self._engines[engine_id]
        logger.info(f"[EngineScheduler] 注销引擎: {engine_id}")
        return True

    def get_available_engine(self, project: Optional[str] = None) -> Optional[Tuple[str, EngineInfo]]:
        """获取可用的引擎
        
        Args:
            project: 优先使用的项目（None 表示使用 TaskWatcher 项目）
            
        Returns:
            (engine_id, EngineInfo) 或 None
        """
        target_project = project or self._default_project
        
        # 首先尝试在指定项目中查找
        if target_project in self._project_engines:
            for engine_id in self._project_engines[target_project]:
                engine_info = self._engines.get(engine_id)
                if engine_info and engine_info.is_available:
                    return (engine_id, engine_info)
        
        # 然后在所有引擎中查找空闲的
        for engine_id, engine_info in self._engines.items():
            if engine_info.is_available:
                return (engine_id, engine_info)
        
        # 最后使用最近未使用的引擎
        if self._engines:
            best = None
            best_time = float('inf')
            for engine_id, engine_info in self._engines.items():
                if engine_info.last_used < best_time:
                    best_time = engine_info.last_used
                    best = (engine_id, engine_info)
            return best
        
        return None

    def allocate_engine(
        self,
        task_id: str,
        project: Optional[str] = None
    ) -> Optional[Tuple[str, Any, Any, str]]:
        """分配一个引擎执行任务
        
        Args:
            task_id: 任务 ID
            project: 项目名称（用于存储会话）
            
        Returns:
            (engine_id, chat_engine, session_manager, session_project) 或 None
        """
        result = self.get_available_engine(project)
        if not result:
            logger.warning("[EngineScheduler] 没有可用的引擎")
            return None
        
        engine_id, engine_info = result
        engine_info.mark_busy(task_id)
        
        session_project = project or self._default_project
        logger.info(f"[EngineScheduler] 分配引擎: {engine_id} -> task={task_id}, project={session_project}")
        
        return (engine_id, engine_info.chat_engine, engine_info.session_manager, session_project)

    def release_engine(self, engine_id: str) -> bool:
        """释放引擎（任务完成后调用）
        
        Args:
            engine_id: 引擎 ID
            
        Returns:
            是否成功
        """
        if engine_id not in self._engines:
            return False
        
        engine_info = self._engines[engine_id]
        engine_info.mark_free()
        
        logger.debug(f"[EngineScheduler] 释放引擎: {engine_id}")
        return True

    def mark_task_completed(self, engine_id: str) -> None:
        """标记任务完成（内部调用）
        
        Args:
            engine_id: 引擎 ID
        """
        self.release_engine(engine_id)

    def get_engine_status(self) -> List[Dict]:
        """获取所有引擎状态
        
        Returns:
            引擎状态列表
        """
        result = []
        for engine_id, engine_info in self._engines.items():
            result.append({
                "engine_id": engine_id,
                "project": engine_info.project,
                "is_busy": engine_info.is_busy,
                "current_task": engine_info.current_task_id,
                "last_used": engine_info.last_used,
            })
        return result

    def get_available_count(self) -> int:
        """获取可用引擎数量"""
        return sum(1 for e in self._engines.values() if e.is_available)

    def get_total_count(self) -> int:
        """获取总引擎数量"""
        return len(self._engines)

    @property
    def default_project(self) -> str:
        """获取默认项目名称"""
        return self._default_project

    def set_default_project(self, project: str) -> None:
        """设置默认项目名称"""
        self._default_project = project

    @property
    def engines(self) -> Dict[str, EngineInfo]:
        """获取所有引擎信息"""
        return self._engines


# 全局单例调度器
_scheduler: Optional[EngineScheduler] = None


def get_engine_scheduler() -> EngineScheduler:
    """获取全局引擎调度器"""
    global _scheduler
    if _scheduler is None:
        _scheduler = EngineScheduler()
    return _scheduler


def reset_engine_scheduler() -> None:
    """重置全局调度器（用于测试）"""
    global _scheduler
    if _scheduler:
        _scheduler._engines.clear()
        _scheduler._project_engines.clear()
    _scheduler = None