# -*- coding: utf-8 -*-
"""
TaskFileManager - 纯文件操作的任务管理器

所有任务相关操作基于文件系统：
- 任务文件: *.task.md
- 结果目录: 任务名_执行时间_uuid/
- 对话记录: conversation.md
- 最终结果: result.md
- 错误日志: error.log

目录结构:
.drifox/tasks/
├── *.task.md              # 任务定义文件
└── 任务名_2026-05-13_abc123/
    ├── conversation.md    # 对话记录
    ├── result.md          # 最终结果
    └── error.log          # 错误日志（如有）
"""
import os
import uuid
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from loguru import logger


# 全局任务文件夹名称
TASKS_FOLDER = "tasks"
RESULTS_FOLDER = "results"
CONFIG_FOLDER = "config"


@dataclass
class TaskFileInfo:
    """任务文件信息"""
    path: str                    # 文件绝对路径
    name: str                    # 文件名（不含路径）
    task_name: str               # 任务名称
    trigger_mode: str            # 触发模式


@dataclass
class ExecutionResult:
    """执行结果"""
    task_id: str                 # 任务ID
    result_dir: str              # 结果目录
    conversation_file: str       # 对话文件路径
    result_file: str            # 结果文件路径
    error_file: Optional[str]    # 错误文件路径（如有）
    success: bool                # 是否成功
    start_time: datetime         # 开始时间
    end_time: datetime           # 结束时间
    duration: float              # 持续时间（秒）


class TaskFileManager:
    """
    纯文件操作的任务管理器
    
    不使用数据库，所有数据通过文件系统管理。
    """

    # 单例实例
    _instance: Optional["TaskFileManager"] = None

    def __init__(self, drifox_dir: Optional[str] = None):
        """
        初始化文件管理器
        
        Args:
            drifox_dir: Drifox 基础目录，默认使用 .drifox
        """
        if drifox_dir:
            self._drifox_dir = os.path.abspath(drifox_dir)
        else:
            # 默认使用项目根目录下的 .drifox
            self._drifox_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                ".drifox"
            )
        
        # 任务目录结构：tasks/config/ 和 tasks/results/
        self._tasks_dir = os.path.join(self._drifox_dir, TASKS_FOLDER)
        self._config_dir = os.path.join(self._tasks_dir, CONFIG_FOLDER)
        self._results_dir = os.path.join(self._tasks_dir, RESULTS_FOLDER)
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保必要目录存在"""
        os.makedirs(self._tasks_dir, exist_ok=True)
        os.makedirs(self._config_dir, exist_ok=True)
        os.makedirs(self._results_dir, exist_ok=True)

    @classmethod
    def get_instance(cls, drifox_dir: Optional[str] = None) -> "TaskFileManager":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls(drifox_dir)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        cls._instance = None

    # ==================== 路径属性 ====================
    
    @property
    def drifox_dir(self) -> str:
        """Drifox 基础目录"""
        return self._drifox_dir

    @property
    def tasks_dir(self) -> str:
        """任务文件夹路径"""
        return self._tasks_dir

    @property
    def config_dir(self) -> str:
        """任务配置文件夹路径"""
        return self._config_dir

    @property
    def results_dir(self) -> str:
        """结果文件夹路径"""
        return self._results_dir

    # ==================== 任务文件操作 ====================

    def get_task_files(self) -> List[TaskFileInfo]:
        """
        扫描所有任务文件
        
        Returns:
            任务文件信息列表
        """
        task_files = []
        pattern = "*.task.md"

        if not os.path.exists(self._config_dir):
            return task_files

        for file_path in Path(self._config_dir).glob(pattern):
            if file_path.is_file():
                try:
                    info = self._parse_task_file_info(str(file_path))
                    if info:
                        task_files.append(info)
                except Exception as e:
                    logger.error(f"[TaskFileManager] 解析任务文件失败: {file_path}, {e}")

        return sorted(task_files, key=lambda x: x.task_name)

    def _parse_task_file_info(self, file_path: str) -> Optional[TaskFileInfo]:
        """
        解析任务文件信息
        
        Args:
            file_path: 文件路径
            
        Returns:
            TaskFileInfo 或 None
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 解析 frontmatter
            lines = content.split('\n')
            task_name = os.path.splitext(os.path.basename(file_path))[0]
            trigger_mode = "manual"

            if '---' in content:
                fm_start = content.index('---')
                fm_end = content.index('---', fm_start + 3)
                fm_text = content[fm_start + 3:fm_end]

                for line in fm_text.split('\n'):
                    line = line.strip()
                    if line.startswith('name:'):
                        task_name = line[5:].strip().strip('"\'')
                    elif line.startswith('trigger:'):
                        # 检查 trigger.mode
                        trigger_mode = "manual"  # 默认值
                    elif line.startswith('mode:'):
                        trigger_mode = line[5:].strip().strip('"\'')

            return TaskFileInfo(
                path=file_path,
                name=os.path.basename(file_path),
                task_name=task_name,
                trigger_mode=trigger_mode
            )
        except Exception as e:
            logger.error(f"[TaskFileManager] 解析任务文件信息失败: {file_path}, {e}")
            return None

    def create_task_file(self, name: str, content: str, trigger_mode: str = "manual") -> str:
        """
        创建任务文件
        
        Args:
            name: 任务名称
            content: 任务内容
            trigger_mode: 触发模式
            
        Returns:
            任务文件路径
        """
        # 生成安全的文件名
        safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in name)
        filename = f"{safe_name}.task.md"
        file_path = os.path.join(self._config_dir, filename)

        # 生成唯一 ID
        task_id = str(uuid.uuid4())

        # 构建文件内容
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        file_content = f"""---
id: {task_id}
name: {name}
trigger:
  mode: {trigger_mode}
created: {now}
---

{content}
"""

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(file_content)

        logger.info(f"[TaskFileManager] 创建任务文件: {file_path}")
        return file_path

    # ==================== 结果目录操作 ====================

    def create_result_dir(self, task_name: str) -> str:
        """
        创建任务结果目录
        
        命名格式: 任务名_日期_时间_uuid
        
        Args:
            task_name: 任务名称
            
        Returns:
            结果目录路径
        """
        now = datetime.now()
        date_str = now.strftime('%Y%m%d')
        time_str = now.strftime('%H-%M-%S')
        unique_id = str(uuid.uuid4())[:8]

        # 清理任务名中的非法字符
        safe_name = "".join(c if c.isalnum() or c in ('_', '-', ' ') else '_' for c in task_name)

        # 组合目录名
        folder_name = f"{safe_name}_{date_str}_{time_str}_{unique_id}"
        result_dir = os.path.join(self._results_dir, folder_name)

        os.makedirs(result_dir, exist_ok=True)

        logger.info(f"[TaskFileManager] 创建结果目录: {result_dir}")
        return result_dir

    def write_conversation(self, result_dir: str, task_name: str, task_content: str,
                          conversation_lines: List[str]) -> str:
        """
        写入对话记录
        
        Args:
            result_dir: 结果目录路径
            task_name: 任务名称
            task_content: 任务原始内容
            conversation_lines: 对话行列表
            
        Returns:
            对话文件路径
        """
        filepath = os.path.join(result_dir, 'conversation.md')
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 构建对话内容
        conversation_text = '\n\n'.join(conversation_lines) if conversation_lines else '(无对话)'

        content = f"""# {task_name} - 对话记录
时间: {now}

---

## 任务内容
{task_content}

---

## 对话记录
{conversation_text}
"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        return filepath

    def append_conversation(self, result_dir: str, role: str, content: str) -> None:
        """
        追加对话内容到现有对话文件
        
        Args:
            result_dir: 结果目录路径
            role: 角色 (user/assistant/system)
            content: 对话内容
        """
        filepath = os.path.join(result_dir, 'conversation.md')
        
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(f"\n\n## {role.capitalize()}\n\n{content}")
        except Exception as e:
            logger.error(f"[TaskFileManager] 追加对话失败: {e}")

    def write_result(self, result_dir: str, task_name: str, result_text: str,
                     status: str = 'completed') -> str:
        """
        写入执行结果
        
        Args:
            result_dir: 结果目录路径
            task_name: 任务名称
            result_text: 结果文本
            status: 状态 'completed' | 'failed'
            
        Returns:
            结果文件路径
        """
        filepath = os.path.join(result_dir, 'result.md')

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        icon = '✅' if status == 'completed' else '❌'

        content = f"""# {task_name} - 执行结果
任务: {task_name}
时间: {now}
状态: {icon} {status.upper()}

---

{result_text}
"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        return filepath

    def write_error(self, result_dir: str, task_name: str, error_msg: str) -> str:
        """
        写入错误日志
        
        Args:
            result_dir: 结果目录路径
            task_name: 任务名称
            error_msg: 错误信息
            
        Returns:
            错误文件路径
        """
        filepath = os.path.join(result_dir, 'error.log')

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        content = f"""# {task_name} - 错误日志
时间: {now}
状态: ❌ 失败

---

## 错误信息
{error_msg}
"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        return filepath

    # ==================== 历史记录查询 ====================

    def list_results(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        扫描历史执行记录
        
        Args:
            limit: 返回数量限制
            
        Returns:
            历史记录列表
        """
        results = []

        if not os.path.exists(self._results_dir):
            return results

        # 扫描结果目录（按修改时间倒序）
        folders = []
        for entry in os.scandir(self._results_dir):
            if entry.is_dir() and '_' in entry.name:
                # 检查是否是结果目录（包含下划线分隔的时间戳和uuid）
                parts = entry.name.rsplit('_', 2)
                if len(parts) >= 2:
                    try:
                        # 验证时间戳格式
                        time_part = parts[-2]
                        if len(time_part) == 8 and time_part.isdigit():
                            mtime = entry.stat().st_mtime
                            folders.append((mtime, entry.name, entry.path))
                    except (ValueError, IndexError):
                        continue

        # 按修改时间倒序
        folders.sort(key=lambda x: x[0], reverse=True)

        for _, folder_name, folder_path in folders[:limit]:
            # 解析任务名（去掉日期_时间_uuid部分）
            task_name = folder_name.rsplit('_', 2)[0]

            # 判断状态
            status = 'unknown'
            result_file = os.path.join(folder_path, 'result.md')
            error_file = os.path.join(folder_path, 'error.log')

            if os.path.exists(result_file):
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if '✅ COMPLETED' in content:
                            status = 'completed'
                        elif '❌ FAILED' in content:
                            status = 'failed'
                except Exception:
                    pass
            elif os.path.exists(error_file):
                status = 'failed'

            results.append({
                'name': task_name,
                'folder_name': folder_name,
                'result_dir': folder_path,
                'status': status,
                'mtime': os.path.getmtime(folder_path),
            })

        return results

    def open_result(self, result_dir: str) -> bool:
        """
        使用系统默认应用打开结果目录
        
        Args:
            result_dir: 结果目录路径
            
        Returns:
            是否成功
        """
        import subprocess
        import sys

        if not os.path.exists(result_dir):
            return False

        try:
            if sys.platform == 'win32':
                os.startfile(result_dir)
            elif sys.platform == 'darwin':
                subprocess.run(['open', result_dir], check=True)
            else:
                subprocess.run(['xdg-open', result_dir], check=True)
            return True
        except Exception as e:
            logger.error(f"[TaskFileManager] 打开结果目录失败: {e}")
            return False

    def open_tasks_folder(self) -> bool:
        """
        打开任务文件夹
        
        Returns:
            是否成功
        """
        return self.open_result(self._tasks_dir)

    def delete_result(self, result_dir: str) -> bool:
        """
        删除结果目录
        
        Args:
            result_dir: 结果目录路径
            
        Returns:
            是否成功
        """
        import shutil

        if not os.path.exists(result_dir):
            return True

        try:
            shutil.rmtree(result_dir)
            logger.info(f"[TaskFileManager] 删除结果目录: {result_dir}")
            return True
        except Exception as e:
            logger.error(f"[TaskFileManager] 删除结果目录失败: {result_dir}, {e}")
            return False

    def get_result_info(self, result_dir: str) -> Optional[Dict[str, Any]]:
        """
        获取结果目录的详细信息
        
        Args:
            result_dir: 结果目录路径
            
        Returns:
            结果信息字典
        """
        if not os.path.exists(result_dir):
            return None

        result_file = os.path.join(result_dir, 'result.md')
        error_file = os.path.join(result_dir, 'error.log')
        conv_file = os.path.join(result_dir, 'conversation.md')

        info = {
            'result_dir': result_dir,
            'has_result': os.path.exists(result_file),
            'has_error': os.path.exists(error_file),
            'has_conversation': os.path.exists(conv_file),
            'files': [],
        }

        try:
            for entry in os.scandir(result_dir):
                info['files'].append({
                    'name': entry.name,
                    'size': entry.stat().st_size,
                    'mtime': datetime.fromtimestamp(entry.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                })
        except Exception as e:
            logger.error(f"[TaskFileManager] 获取结果信息失败: {result_dir}, {e}")

        return info