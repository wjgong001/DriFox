# -*- coding: utf-8 -*-
"""
Shell 执行任务 - 异步执行系统命令
"""

import subprocess
import platform
import threading
from typing import Callable, Optional


def execute_shell(
    command: str,
    callback: Callable[[str], None],
    timeout: int = 120,
) -> None:
    """
    在后台线程执行 Shell 命令。
    
    Args:
        command: 要执行的命令
        callback: 执行完成后的回调函数，接收命令输出作为参数
        timeout: 超时时间（秒）
    """
    def _run():
        try:
            cmd = command
            system = platform.system()
            # Windows: 强制切换到 UTF-8 代码页
            if system == "Windows":
                cmd = f"chcp 65001 >nul 2>&1 && {command}"

            res = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
            )
            output = res.stdout.strip() if res.stdout else ""
            error_out = res.stderr.strip() if res.stderr else ""
            combined = "\n".join(filter(None, [output, error_out]))
            result_text = combined if combined else "(命令执行完成，无输出)"
        except subprocess.TimeoutExpired:
            result_text = "[错误] 命令执行超时"
        except Exception as e:
            result_text = f"[错误] {str(e)}"

        callback(result_text)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


class ShellExecutionTask:
    """Shell 命令执行任务 - 替代 QRunnable 版本"""

    def __init__(
        self,
        command: str,
        callback: Callable[[str], None],
        timeout: int = 120,
    ):
        self.command = command
        self.callback = callback
        self.timeout = timeout
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动后台执行"""
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        """执行命令"""
        try:
            cmd = self.command
            system = platform.system()
            # Windows: 强制切换到 UTF-8 代码页
            if system == "Windows":
                cmd = f"chcp 65001 >nul 2>&1 && {self.command}"

            res = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=self.timeout,
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

    def cancel(self) -> None:
        """取消任务（设置线程为 daemon 会在主线程结束时自动终止）"""
        # 注意：subprocess 无法直接终止，但 daemon=True 会让线程随主进程结束
        pass