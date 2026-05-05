# -*- coding: utf-8 -*-
"""
输出处理器
处理任务执行结果的输出，支持文件、剪贴板、通知、webhook 等模式
"""

import os
import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from .models import TaskConfig, TaskResult, OutputMode, OutputFormat


class OutputHandler:
    """输出处理器
    
    根据 output.mode 分发任务执行结果
    """

    def __init__(self):
        """初始化输出处理器"""
        self._webhook_cache: Dict[str, Any] = {}  # 简单的 webhook 配置缓存

    def handle(self, config: TaskConfig, result: TaskResult) -> bool:
        """处理任务执行结果
        
        Args:
            config: 任务配置
            result: 任务执行结果
            
        Returns:
            是否成功
        """
        output_mode = config.output.mode
        
        if output_mode == OutputMode.FILE:
            return self._output_to_file(config, result)
        elif output_mode == OutputMode.CLIPBOARD:
            return self._output_to_clipboard(result)
        elif output_mode == OutputMode.NOTIFICATION:
            return self._output_to_notification(config, result)
        elif output_mode == OutputMode.WEBHOOK:
            return self._output_to_webhook(config, result)
        else:
            logger.warning(f"[OutputHandler] 未知的输出模式: {output_mode}")
            return False

    def _output_to_file(self, config: TaskConfig, result: TaskResult) -> bool:
        """输出到文件
        
        Args:
            config: 任务配置
            result: 任务执行结果
            
        Returns:
            是否成功
        """
        destination = config.output.destination
        if not destination:
            logger.warning("[OutputHandler] 未配置输出路径")
            return False
        
        try:
            # 替换路径中的占位符
            destination = self._expand_destination(destination, config)
            
            # 确保目录存在
            output_dir = os.path.dirname(destination)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            
            # 获取输出内容
            content = result.output_content or ""
            
            # 根据格式处理内容
            if config.output.format == OutputFormat.JSON:
                content = self._to_json_content(content)
            elif config.output.format == OutputFormat.TEXT:
                content = self._to_plain_text(content)
            # markdown 格式保持原样
            
            # 写入文件
            with open(destination, "w", encoding="utf-8") as f:
                f.write(content)
            
            logger.info(f"[OutputHandler] 输出到文件: {destination}")
            
            # 更新结果的 output_file
            result.output_file = destination
            
            return True
        except Exception as e:
            logger.exception(f"[OutputHandler] 输出到文件失败: {e}")
            return False

    def _expand_destination(self, destination: str, config: TaskConfig) -> str:
        """展开路径中的占位符
        
        Args:
            destination: 输出路径模板
            config: 任务配置
            
        Returns:
            展开后的路径
        """
        # 替换 {filename} 为源文件名
        if config.source_file:
            source_name = os.path.splitext(os.path.basename(config.source_file))[0]
            destination = destination.replace("{filename}", source_name)
        
        # 替换 {task_id} 为任务 ID
        destination = destination.replace("{task_id}", config.id)
        
        # 替换 {date} 为当前日期
        from datetime import datetime
        now = datetime.now()
        destination = destination.replace("{date}", now.strftime("%Y-%m-%d"))
        destination = destination.replace("{time}", now.strftime("%H-%M-%S"))
        destination = destination.replace("{datetime}", now.strftime("%Y-%m-%d_%H-%M-%S"))
        
        return destination

    def _to_json_content(self, content: str) -> str:
        """将内容转换为 JSON 格式
        
        Args:
            content: 原始内容
            
        Returns:
            JSON 格式的内容
        """
        try:
            # 如果内容是 JSON 字符串，解析后重新格式化为美化输出
            data = json.loads(content)
            return json.dumps(data, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            # 如果不是 JSON，包装为 JSON 对象
            return json.dumps({
                "content": content,
                "timestamp": self._get_timestamp()
            }, ensure_ascii=False, indent=2)

    def _to_plain_text(self, content: str) -> str:
        """将 Markdown 内容转换为纯文本
        
        Args:
            content: Markdown 内容
            
        Returns:
            纯文本内容
        """
        import re
        
        # 移除代码块标记
        content = re.sub(r"```[\w]*\n?", "", content)
        
        # 移除行内代码标记
        content = re.sub(r"`([^`]+)`", r"\1", content)
        
        # 移除标题标记（保留文字）
        content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
        
        # 移除加粗和斜体标记
        content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
        content = re.sub(r"\*(.+?)\*", r"\1", content)
        content = re.sub(r"__(.+?)__", r"\1", content)
        content = re.sub(r"_(.+?)_", r"\1", content)
        
        # 移除链接，保留文字
        content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
        
        # 移除图片
        content = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", content)
        
        # 移除列表标记
        content = re.sub(r"^[-*+]\s+", "- ", content, flags=re.MULTILINE)
        content = re.sub(r"^\d+\.\s+", "- ", content, flags=re.MULTILINE)
        
        # 移除水平线
        content = re.sub(r"^[-*_]{3,}$", "", content, flags=re.MULTILINE)
        
        return content.strip()

    def _output_to_clipboard(self, result: TaskResult) -> bool:
        """输出到剪贴板
        
        Args:
            result: 任务执行结果
            
        Returns:
            是否成功
        """
        content = result.output_content
        if not content:
            logger.warning("[OutputHandler] 无内容可复制到剪贴板")
            return False
        
        try:
            # 尝试使用 PyQt5
            try:
                from PyQt5.QtWidgets import QApplication
                from PyQt5.QtGui import QClipboard
                
                clipboard = QApplication.clipboard()
                clipboard.setText(content)
                logger.info("[OutputHandler] 已复制到剪贴板")
                return True
            except ImportError:
                pass
            
            # 尝试使用 tkinter
            try:
                import tkinter as tk
                root = tk.Tk()
                root.withdraw()
                root.clipboard_clear()
                root.clipboard_append(content)
                root.update()
                root.destroy()
                logger.info("[OutputHandler] 已复制到剪贴板 (tkinter)")
                return True
            except ImportError:
                pass
            
            # 尝试使用 pyperclip
            try:
                import pyperclip
                pyperclip.copy(content)
                logger.info("[OutputHandler] 已复制到剪贴板 (pyperclip)")
                return True
            except ImportError:
                pass
            
            logger.error("[OutputHandler] 无法复制到剪贴板：无可用的剪贴板库")
            return False
            
        except Exception as e:
            logger.error(f"[OutputHandler] 复制到剪贴板失败: {e}")
            return False

    def _output_to_notification(self, config: TaskConfig, result: TaskResult) -> bool:
        """发送系统通知
        
        Args:
            config: 任务配置
            result: 任务执行结果
            
        Returns:
            是否成功
        """
        title = config.name or f"任务 {config.id[:8]} 完成"
        
        if result.success:
            content = f"执行成功"
            if result.output_content:
                # 截取前100个字符
                content += f"\n{result.output_content[:100]}..."
        else:
            content = f"执行失败: {result.error}"
        
        try:
            # 尝试使用 plyer
            try:
                from plyer import notification
                notification.notify(
                    title=title,
                    message=content,
                    app_name="Drifox",
                    timeout=10
                )
                logger.info("[OutputHandler] 已发送系统通知")
                return True
            except ImportError:
                pass
            
            # 尝试使用 plyanuts
            try:
                from plyanuts import Notification
                notif = Notification(title=title, message=content)
                notif.show()
                logger.info("[OutputHandler] 已发送系统通知 (plyanuts)")
                return True
            except ImportError:
                pass
            
            # 尝试使用 PyQt5
            try:
                from PyQt5.QtWidgets import QApplication
                from PyQt5.QtCore import QTimer
                
                # 使用系统托盘气泡
                # 注意：这需要 QSystemTrayIcon
                logger.warning("[OutputHandler] PyQt5 通知需要额外的托盘图标支持")
                return False
            except ImportError:
                pass
            
            logger.error("[OutputHandler] 无法发送通知：无可用的通知库")
            return False
            
        except Exception as e:
            logger.error(f"[OutputHandler] 发送通知失败: {e}")
            return False

    def _output_to_webhook(self, config: TaskConfig, result: TaskResult) -> bool:
        """POST 到 webhook
        
        Args:
            config: 任务配置
            result: 任务执行结果
            
        Returns:
            是否成功
        """
        url = config.output.destination
        if not url:
            logger.warning("[OutputHandler] 未配置 webhook URL")
            return False
        
        # 构建 webhook 数据
        data = {
            "task_id": config.id,
            "task_name": config.name,
            "status": "success" if result.success else "failed",
            "output_content": result.output_content,
            "error": result.error,
            "execution_time": result.execution_time,
            "timestamp": self._get_timestamp(),
        }
        
        try:
            import requests
            
            response = requests.post(
                url,
                json=data,
                timeout=30,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code < 400:
                logger.info(f"[OutputHandler] Webhook 发送成功: {url}")
                return True
            else:
                logger.warning(f"[OutputHandler] Webhook 返回错误: {response.status_code}")
                return False
                
        except ImportError:
            logger.error("[OutputHandler] requests 库未安装，无法发送 webhook")
            return False
        except Exception as e:
            logger.error(f"[OutputHandler] Webhook 发送失败: {e}")
            return False

    def _get_timestamp(self) -> str:
        """获取当前时间戳"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_output_file(
        self,
        content: str,
        file_path: str,
        format: OutputFormat = OutputFormat.MARKDOWN
    ) -> bool:
        """手动保存输出文件
        
        Args:
            content: 内容
            file_path: 文件路径
            format: 输出格式
            
        Returns:
            是否成功
        """
        try:
            # 确保目录存在
            output_dir = os.path.dirname(file_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            
            # 根据格式处理内容
            if format == OutputFormat.JSON:
                content = self._to_json_content(content)
            elif format == OutputFormat.TEXT:
                content = self._to_plain_text(content)
            
            # 写入文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            
            logger.info(f"[OutputHandler] 保存文件: {file_path}")
            return True
        except Exception as e:
            logger.error(f"[OutputHandler] 保存文件失败: {e}")
            return False

    def read_output_file(self, file_path: str) -> Optional[str]:
        """读取输出文件
        
        Args:
            file_path: 文件路径
            
        Returns:
            文件内容或 None
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"[OutputHandler] 读取文件失败: {e}")
            return None