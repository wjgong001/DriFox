# -*- coding: utf-8 -*-
"""
TaskExecutor - 任务执行器

使用 ChatWorker 直接执行 LLM 任务（QThread 信号驱动，无需等待）
- 跳过 ChatEngine/SessionManager
- 结果写入文件
"""
import time
import threading
from typing import Optional, Callable, Dict, Any, List

from loguru import logger
from .models import TaskConfig
from .file_manager import TaskFileManager


class TaskExecutor:
    """
    任务执行器
    """

    def __init__(self, get_model_config: Optional[Callable] = None,
                 tool_executor: Optional[Any] = None):
        self._file_manager = TaskFileManager.get_instance()
        self._callbacks: Dict[str, Callable] = {}
        self._running_tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor

    def set_model_config_provider(self, provider: Callable):
        self._get_model_config = provider

    def set_tool_executor(self, executor):
        self._tool_executor = executor

    def set_callback(self, event: str, callback: Callable) -> None:
        self._callbacks[event] = callback

    def _emit(self, event: str, *args) -> None:
        callback = self._callbacks.get(event)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[TaskExecutor] 回调错误: {e}")

    def execute(self, config: TaskConfig) -> str:
        """执行任务 - 直接创建 ChatWorker（QThread，自带事件循环）"""
        result_dir = self._file_manager.create_result_dir(config.name)

        ctx = {
            'config': config,
            'result_dir': result_dir,
            'start_time': time.time(),
            'status': 'running',
            'result_text': '',
        }

        with self._lock:
            self._running_tasks[config.id] = ctx

        logger.info(f"[TaskExecutor] 开始执行: {config.name}, result_dir={result_dir}")
        self._emit('task_started', config.id, config.name)

        task_content = config.content or "(无内容)"
        self._file_manager.write_conversation(
            result_dir, config.name, task_content,
            [f"## 用户\n\n{task_content}\n"]
        )

        llm_config = self._get_model_config() if self._get_model_config else {}
        if not llm_config or not llm_config.get("API_KEY"):
            # 无模型配置，模拟执行
            self._start_simulated(config, result_dir)
        else:
            self._start_worker(config, task_content, llm_config, result_dir)

        return result_dir

    def _start_simulated(self, config: TaskConfig, result_dir: str):
        """模拟执行（无模型配置时）"""
        import threading as _t
        def run():
            _t.current_thread().name = f"task-sim-{config.id[:8]}"
            _t.sleep(2)
            result_text = f"# {config.name}\n\n任务已执行完成。\n\n## 任务内容\n\n{config.content or '(无内容)'}\n"
            self._file_manager.write_result(result_dir, config.name, result_text, 'completed')
            ctx = self._running_tasks.get(config.id)
            elapsed = time.time() - ctx['start_time'] if ctx else 0
            logger.info(f"[TaskExecutor] 完成(模拟): {config.name}, 耗时: {elapsed:.1f}s")
            self._emit('task_completed', config.id, config.name)
            with self._lock:
                if config.id in self._running_tasks:
                    del self._running_tasks[config.id]
        _t.Thread(target=run, daemon=True).start()

    def _start_worker(self, config: TaskConfig, task_content: str,
                      llm_config: Dict, result_dir: str):
        """使用 OpenAI API 同步调用（后台线程直接执行）"""
        import threading as _t

        def run():
            _t.current_thread().name = f"task-llm-{config.id[:8]}"
            try:
                self._emit('task_progress', config.id, config.name, "连接模型中...")

                from openai import OpenAI
                client = OpenAI(
                    api_key=llm_config.get("API_KEY", ""),
                    base_url=llm_config.get("API_URL", ""),
                )
                model = llm_config.get("模型名称", "gpt-4o-mini")

                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": task_content}],
                    stream=True,
                    timeout=300,
                )

                collected = []
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        text = chunk.choices[0].delta.content
                        collected.append(text)
                        msg = ''.join(collected[-20:])
                        self._emit('task_progress', config.id, config.name, msg[:120])
                        self._file_manager.append_conversation(result_dir, "assistant", text)

                result = ''.join(collected)
                self._file_manager.write_result(result_dir, config.name, result, 'completed')
                ctx = self._running_tasks.get(config.id)
                elapsed = time.time() - ctx['start_time'] if ctx else 0
                logger.info(f"[TaskExecutor] 完成: {config.name}, 耗时: {elapsed:.1f}s")
                self._emit('task_completed', config.id, config.name)

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[TaskExecutor] 失败: {config.name}, error={error_msg}")
                self._file_manager.write_error(result_dir, config.name, error_msg)
                self._emit('task_failed', config.id, config.name, error_msg)
            finally:
                with self._lock:
                    if config.id in self._running_tasks:
                        del self._running_tasks[config.id]

        _t.Thread(target=run, daemon=True, name=f"task-{config.id[:8]}").start()

    def get_running_tasks(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return self._running_tasks.copy()

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._running_tasks

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._running_tasks:
                return False
            ctx = self._running_tasks[task_id]
            task_name = ctx['config'].name
        self._file_manager.write_error(ctx['result_dir'], task_name, "任务被取消")
        with self._lock:
            del self._running_tasks[task_id]
        logger.info(f"[TaskExecutor] 已取消: {task_name}")
        self._emit('task_cancelled', task_id, task_name)
        return True