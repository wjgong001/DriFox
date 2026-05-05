# -*- coding: utf-8 -*-
"""
任务配置存储
直接从文件系统读取任务配置，不再使用 SQLite
"""

import os
from typing import List, Optional, Dict, Any
from loguru import logger

from .parser import TaskParser
from .models import TaskConfig, TriggerMode


class TaskConfigStore:
    """任务配置存储
    
    直接从文件系统读取任务配置（.drifox/tasks/*.task.md）
    删除文件即删除任务，不再使用 SQLite
    """

    def __init__(self, tasks_dir: Optional[str] = None):
        """初始化任务配置存储
        
        Args:
            tasks_dir: 任务目录，默认使用 .drifox/tasks
        """
        self._tasks_dir = tasks_dir or os.path.join(".drifox", "tasks")
        self._parser = TaskParser()
        self._cache: Dict[str, TaskConfig] = {}
        self._file_mtimes: Dict[str, float] = {}
        self._scan_files()
    
    def _scan_files(self):
        """扫描任务目录，更新缓存"""
        self._cache.clear()
        self._file_mtimes.clear()
        
        if not os.path.exists(self._tasks_dir):
            return
        
        for file in os.listdir(self._tasks_dir):
            if not file.endswith(".task.md"):
                continue
            
            file_path = os.path.join(self._tasks_dir, file)
            try:
                config = self._parser.parse_file(file_path)
                if config:
                    mtime = os.path.getmtime(file_path)
                    self._cache[config.id] = config
                    self._file_mtimes[config.id] = mtime
            except Exception as e:
                logger.warning(f"[TaskConfigStore] 解析任务文件失败: {file}, {e}")
    
    def _check_cache_valid(self) -> bool:
        """检查缓存是否有效（文件未变化）"""
        if not os.path.exists(self._tasks_dir):
            return len(self._cache) == 0
        
        current_files = {
            f for f in os.listdir(self._tasks_dir) if f.endswith(".task.md")
        }
        
        # 检查是否有新文件或修改的文件
        cached_files = set(self._file_mtimes.keys())
        
        # 新文件或删除的文件
        if current_files != cached_files:
            return False
        
        # 检查修改时间
        for task_id, cached_mtime in self._file_mtimes.items():
            if task_id not in self._cache:
                return False
            config = self._cache[task_id]
            if not config or not config.source_file:
                return False
            try:
                current_mtime = os.path.getmtime(config.source_file)
                if current_mtime > cached_mtime:
                    return False
            except Exception:
                pass
        
        return True
    
    def load_all(self) -> List[TaskConfig]:
        """加载所有任务配置
        
        Returns:
            任务配置列表
        """
        # 如果缓存无效，重新扫描
        if not self._check_cache_valid():
            self._scan_files()
        
        configs = list(self._cache.values())
        logger.debug(f"[TaskConfigStore] 加载了 {len(configs)} 个任务配置")
        return configs

    def get(self, task_id: str) -> Optional[TaskConfig]:
        """获取单个任务配置
        
        Args:
            task_id: 任务 ID
            
        Returns:
            任务配置或 None
        """
        # 如果缓存无效，重新扫描
        if not self._check_cache_valid():
            self._scan_files()
        
        return self._cache.get(task_id)

    def save(self, config: TaskConfig) -> bool:
        """保存任务配置到文件
        
        Args:
            config: 任务配置
            
        Returns:
            是否成功
        """
        try:
            file_path = config.source_file
            if not file_path:
                # 生成文件路径
                safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in (config.name or "task"))[:30]
                file_path = os.path.join(self._tasks_dir, f"{safe_name}_{config.id[:8]}.task.md")
            
            # 构建文件内容
            lines = [
                "---",
                f"id: {config.id}",
            ]
            
            if config.name:
                lines.append(f"name: {config.name}")
            if config.type:
                type_val = config.type.value if hasattr(config.type, 'value') else str(config.type)
                lines.append(f"type: {type_val}")
            
            # trigger
            lines.append("trigger:")
            lines.append(f"  mode: {config.trigger.mode.value}")
            if config.trigger.cron:
                lines.append(f'  cron: "{config.trigger.cron}"')
            
            # context
            lines.append("context:")
            lines.append(f"  session_mode: {config.context.session_mode.value}")
            if config.context.agent:
                lines.append(f"  agent: {config.context.agent}")
            
            # output
            lines.append("output:")
            lines.append(f"  mode: {config.output.mode.value}")
            if config.output.destination:
                lines.append(f"  destination: {config.output.destination}")
            
            lines.append(f"priority: {config.priority}")
            lines.append(f"retry: {config.retry}")
            
            lines.append("---")
            if config.content:
                lines.append("")
                lines.append(config.content)
            
            # 确保目录存在
            os.makedirs(self._tasks_dir, exist_ok=True)
            
            # 写入文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            
            # 更新缓存
            config.source_file = os.path.abspath(file_path)
            self._cache[config.id] = config
            self._file_mtimes[config.id] = os.path.getmtime(file_path)
            
            logger.debug(f"[TaskConfigStore] 保存任务配置: {config.id} -> {file_path}")
            return True
        except Exception as e:
            logger.error(f"[TaskConfigStore] 保存任务配置失败: {e}")
            return False

    def delete(self, task_id: str) -> bool:
        """删除任务（删除文件）
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        config = self._cache.get(task_id)
        if not config or not config.source_file:
            return False
        
        try:
            if os.path.exists(config.source_file):
                os.remove(config.source_file)
            
            del self._cache[task_id]
            if task_id in self._file_mtimes:
                del self._file_mtimes[task_id]
            
            logger.debug(f"[TaskConfigStore] 删除任务配置: {task_id}")
            return True
        except Exception as e:
            logger.error(f"[TaskConfigStore] 删除任务配置失败: {e}")
            return False

    def enable(self, task_id: str) -> bool:
        """启用任务（删除 .disabled 后缀）"""
        return True  # 文件系统中没有启用/禁用状态

    def disable(self, task_id: str) -> bool:
        """禁用任务（添加 .disabled 后缀）"""
        config = self._cache.get(task_id)
        if not config or not config.source_file:
            return False
        
        try:
            disabled_path = config.source_file + ".disabled"
            os.rename(config.source_file, disabled_path)
            
            if task_id in self._cache:
                self._cache[task_id].source_file = disabled_path
            
            logger.debug(f"[TaskConfigStore] 禁用任务: {task_id}")
            return True
        except Exception as e:
            logger.error(f"[TaskConfigStore] 禁用任务失败: {e}")
            return False

    def get_by_trigger_mode(self, mode: TriggerMode) -> List[TaskConfig]:
        """获取指定触发模式的任务配置
        
        Args:
            mode: 触发模式
            
        Returns:
            任务配置列表
        """
        configs = self.load_all()
        return [c for c in configs if c.trigger.mode == mode]

    def import_from_file(self, file_path: str) -> Optional[TaskConfig]:
        """从文件导入任务配置
        
        Args:
            file_path: 任务文件路径
            
        Returns:
            导入的任务配置或 None
        """
        try:
            config = self._parser.parse_file(file_path)
            if config:
                self._cache[config.id] = config
                self._file_mtimes[config.id] = os.path.getmtime(file_path)
            return config
        except Exception as e:
            logger.error(f"[TaskConfigStore] 导入任务文件失败: {e}")
            return None

    def export_to_file(self, task_id: str, file_path: str) -> bool:
        """导出任务配置到文件
        
        Args:
            task_id: 任务 ID
            file_path: 目标文件路径
            
        Returns:
            是否成功
        """
        config = self.get(task_id)
        if not config:
            return False
        
        # 复用 save 方法导出
        original_source = config.source_file
        config.source_file = file_path
        result = self.save(config)
        config.source_file = original_source
        return result