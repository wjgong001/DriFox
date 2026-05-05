# -*- coding: utf-8 -*-
"""
任务文件解析器
解析 Markdown 格式的任务文件，支持 YAML frontmatter
"""

import re
import os
from typing import Optional, Tuple, Dict, Any
from loguru import logger

from .models import TaskConfig, TriggerMode, TriggerConfig, ContextConfig, OutputConfig


class TaskParseError(Exception):
    """任务解析错误"""
    pass


class TaskParser:
    """任务文件解析器
    
    解析 Markdown 格式的任务文件，格式如下：
    
    ```markdown
    ---
    id: task_uuid_here
    name: 每日新闻摘要
    type: research
    trigger:
      mode: scheduled
      cron: "0 8 * * *"
    context:
      session_mode: new
      agent: plan
    output:
      mode: file
      destination: "./results/news_summary.md"
    priority: normal
    retry: 3
    timeout: 3600
    ---
    
    # 任务描述
    
    请帮我总结今天的 AI 行业最新动态
    ```
    """

    FRONTMATTER_PATTERN = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n",
        re.DOTALL
    )
    
    # 文件缓存：path -> (mtime, config)
    _file_cache: Dict[str, Tuple[float, TaskConfig]] = {}
    
    def parse_file(self, file_path: str) -> Optional[TaskConfig]:
        """
        解析任务文件（带缓存）
        
        Args:
            file_path: 任务文件路径
            
        Returns:
            TaskConfig 或 None（解析失败）
        """
        abs_path = os.path.abspath(file_path)
        
        # 检查缓存
        if abs_path in self._file_cache:
            cached_mtime, cached_config = self._file_cache[abs_path]
            try:
                current_mtime = os.path.getmtime(abs_path)
                if current_mtime <= cached_mtime:
                    logger.debug(f"[TaskParser] 使用缓存: {abs_path}")
                    return cached_config
            except OSError:
                # 文件可能已删除，清除缓存
                del self._file_cache[abs_path]
        
        # 需要解析
        try:
            config = self._parse_file_uncached(abs_path)
            if config:
                # 更新缓存
                try:
                    mtime = os.path.getmtime(abs_path)
                    self._file_cache[abs_path] = (mtime, config)
                except OSError:
                    pass
            return config
        except Exception as e:
            logger.error(f"[TaskParser] 解析文件失败: {abs_path}, {e}")
            return None
    
    def _parse_file_uncached(self, file_path: str) -> Optional[TaskConfig]:
        """
        不使用缓存的解析方法
        
        Args:
            file_path: 任务文件路径
            
        Returns:
            TaskConfig 或 None
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            return self.parse_content(content, source_file=file_path)
        except FileNotFoundError:
            logger.error(f"[TaskParser] 文件不存在: {file_path}")
            raise TaskParseError(f"文件不存在: {file_path}")
        except Exception as e:
            logger.error(f"[TaskParser] 解析文件失败 {file_path}: {e}")
            raise TaskParseError(f"解析文件失败: {e}")

    def parse_content(self, content: str, source_file: Optional[str] = None) -> Optional[TaskConfig]:
        """
        解析任务内容
        
        Args:
            content: 任务文件内容
            source_file: 源文件路径（可选）
            
        Returns:
            TaskConfig 或 None
        """
        try:
            # 解析 frontmatter
            frontmatter_data, body = self._parse_frontmatter(content)
            
            if not frontmatter_data:
                logger.warning("[TaskParser] 未找到有效的 frontmatter")
                return None
            
            # 设置 id
            task_id = frontmatter_data.get("id")
            if not task_id:
                import uuid
                task_id = str(uuid.uuid4())
            
            # 创建任务配置
            task_config = TaskConfig(
                id=task_id,
                name=frontmatter_data.get("name"),
                type=frontmatter_data.get("type", "custom"),
                trigger=TriggerConfig.from_dict(frontmatter_data.get("trigger", {})),
                context=ContextConfig.from_dict(frontmatter_data.get("context", {})),
                output=OutputConfig.from_dict(frontmatter_data.get("output", {})),
                priority=frontmatter_data.get("priority", "normal"),
                retry=frontmatter_data.get("retry", 0),
                timeout=frontmatter_data.get("timeout", 3600),
            )
            
            # 设置 body 内容
            if body and body.strip():
                task_config.content = body.strip()
            
            # 设置源文件路径
            if source_file:
                task_config.source_file = os.path.abspath(source_file)
            
            logger.debug(f"[TaskParser] 解析成功: {task_config.id}, name={task_config.name}")
            return task_config
            
        except TaskParseError:
            raise
        except Exception as e:
            logger.error(f"[TaskParser] 解析内容失败: {e}")
            raise TaskParseError(f"解析内容失败: {e}")

    def _parse_frontmatter(self, content: str) -> Tuple[Optional[dict], str]:
        """
        解析 YAML frontmatter
        
        Args:
            content: 文件内容
            
        Returns:
            (frontmatter_dict, body) 元组
        """
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return None, content
        
        yaml_str = match.group(1).strip()
        body = content[match.end():]
        
        # 解析 YAML
        try:
            import yaml
            data = yaml.safe_load(yaml_str)
            return data, body
        except ImportError:
            # 如果没有 pyyaml，手动解析简单的 YAML
            logger.warning("[TaskParser] pyyaml 未安装，使用简单解析器")
            return self._simple_yaml_parse(yaml_str), body
        except yaml.YAMLError as e:
            logger.error(f"[TaskParser] YAML 解析错误: {e}")
            raise TaskParseError(f"YAML 格式错误: {e}")

    def _simple_yaml_parse(self, yaml_str: str) -> dict:
        """
        简单的 YAML 解析器（处理基本格式）
        
        Args:
            yaml_str: YAML 字符串
            
        Returns:
            解析后的字典
        """
        result = {}
        current_key = None
        current_dict = result
        stack = [result]
        
        for line in yaml_str.split("\n"):
            line = line.rstrip()
            
            # 空行
            if not line.strip():
                continue
            
            # 计算缩进
            indent = len(line) - len(line.lstrip())
            
            # 检查是否是新键值对
            if ":" in line:
                key_end = line.index(":")
                key = line[:key_end].strip()
                value = line[key_end + 1:].strip()
                
                # 判断是否是嵌套对象
                if not value and indent > 0:
                    # 创建嵌套字典
                    new_dict = {}
                    current_dict[key] = new_dict
                    stack.append(current_dict)
                    current_dict = new_dict
                elif value:
                    # 简单键值对
                    # 尝试解析类型
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    elif value.lower() == "true":
                        value = True
                    elif value.lower() == "false":
                        value = False
                    elif value.isdigit():
                        value = int(value)
                    
                    # 回到正确的层级
                    while stack and indent <= list(reversed(stack)).index(current_dict) if current_dict in stack else 0:
                        if len(stack) > 1:
                            stack.pop()
                            current_dict = stack[-1]
                    
                    current_dict[key] = value
        
        return result
    
    @classmethod
    def clear_cache(cls):
        """清除缓存"""
        cls._file_cache.clear()
        logger.debug("[TaskParser] 缓存已清除")