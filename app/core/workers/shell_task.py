# -*- coding: utf-8 -*-
"""
Shell 执行任务 - 异步执行系统命令
"""

from PyQt5.QtCore import QRunnable, pyqtSlot


class ShellExecutionTask(QRunnable):
    """异步执行Shell命令任务"""

    def __init__(self, command: str, callback):
        # DEPRECATED: QRunnable 只用于 PyQt5 环境
        super().__init__()
        self.command = command
        self.callback = callback
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        # DEPRECATED: 迁移到 async_shell_task.py 的 execute_shell 函数
        import subprocess
        import platform

        try:
            command = self.command
            system = platform.system()
            # Windows: 强制切换到 UTF-8 代码页
            if system == "Windows":
                command = f"chcp 65001 >nul 2>&1 && {command}"

            res = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=120,
            )
            output = res.stdout.strip() if res.stdout else ""
            error_out = res.stderr.strip() if res.stderr else ""
            combined = "\n".join(filter(None, [output, error_out]))
            result_text = combined if combined else "(命令执行完成，无输出)"
        except subprocess.TimeoutExpired:
            result_text = "[错误] 命令执行超时"
        except Exception as e:
            result_text = f"[错误] {str(e)}"

        self.callback(result_text)