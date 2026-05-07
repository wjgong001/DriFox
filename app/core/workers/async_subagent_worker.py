# -*- coding: utf-8 -*-
"""
异步子智能体执行器 - 使用 threading + 回调函数替代 QThread + pyqtSignal
"""

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from app.core.provider_profile import get_provider_profile
from app.core.retry_helper import create_api_call_with_retry

logger = logging.getLogger(__name__)


class AsyncSubAgentExecutor:
    """
    异步子智能体执行器 - 独立线程运行子智能体任务

    使用 threading.Thread 替代 QThread，回调函数替代 pyqtSignal。
    """

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        llm_config: Dict,
        agent_manager: Any,
        tool_executor: Any = None,
        parent_context: str = "",
        is_subagent_call: bool = True,
        # 回调函数替代 pyqtSignal
        on_finished: Callable[[str, str], None] = None,  # task_id, result
        on_error: Callable[[str, str], None] = None,  # task_id, error
        on_progress: Callable[[str, str], None] = None,  # task_id, message
        on_tool_call_started: Callable[[str, str, dict], None] = None,  # task_id, tool_name, args
        on_tool_result: Callable[[str, str, str, bool], None] = None,  # task_id, tool_name, result, success
    ):
        """
        Args:
            task_id: 任务 ID
            agent_name: 智能体名称
            task_description: 任务描述
            llm_config: LLM 配置
            agent_manager: 智能体管理器
            tool_executor: 工具执行器
            parent_context: 父任务上下文
            is_subagent_call: 是否为子智能体调用
            on_finished: 任务完成回调 (task_id, result)
            on_error: 错误回调 (task_id, error)
            on_progress: 进度回调 (task_id, message)
            on_tool_call_started: 工具调用开始回调 (task_id, tool_name, args)
            on_tool_result: 工具结果回调 (task_id, tool_name, result, success)
        """
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_description = task_description
        self.llm_config = llm_config
        self.agent_manager = agent_manager
        self.tool_executor = tool_executor
        self.parent_context = parent_context
        self.is_subagent_call = is_subagent_call

        # 回调函数
        self._on_finished = on_finished
        self._on_error = on_error
        self._on_progress = on_progress
        self._on_tool_call_started = on_tool_call_started
        self._on_tool_result = on_tool_result

        # 内部状态
        self._thread: Optional[threading.Thread] = None
        self._is_cancelled = False
        self._pending_answer: Optional[str] = None
        self._pending_answer_lock = threading.Lock()
        self._last_result: Optional[str] = None
        self._execution_error: Optional[str] = None
        self._start_time: Optional[float] = None

        # 日志存储
        self._logs: List[Dict] = []
        self._log_store_callback: Optional[Callable] = None
        self._get_history_messages: Optional[Callable] = None

        self._question_pending: Optional[Dict] = None

    def set_log_store_callback(self, callback: Callable):
        """设置日志存储回调"""
        self._log_store_callback = callback

    def set_history_getter(self, getter: Callable):
        """设置获取历史消息的回调"""
        self._get_history_messages = getter

    def cancel(self):
        """取消任务"""
        self._is_cancelled = True

    def provide_answer(self, answer: str):
        """提供问题的答案"""
        with self._pending_answer_lock:
            self._pending_answer = answer

    def _emit_finished(self, result: str):
        """发射完成信号"""
        if self._on_finished:
            self._on_finished(self.task_id, result)

    def _emit_error(self, error: str):
        """发射错误信号"""
        if self._on_error:
            self._on_error(self.task_id, error)

    def _emit_progress(self, message: str):
        """发射进度信号"""
        if self._on_progress:
            self._on_progress(self.task_id, message)

    def _emit_tool_call_started(self, tool_name: str, args: dict):
        """发射工具调用开始信号"""
        if self._on_tool_call_started:
            self._on_tool_call_started(self.task_id, tool_name, args)

    def _emit_tool_result(self, tool_name: str, result: str, success: bool):
        """发射工具结果信号"""
        if self._on_tool_result:
            self._on_tool_result(self.task_id, tool_name, result, success)

    def _add_log(self, log_type: str, content: str, extra: dict = None):
        """记录日志"""
        log_entry = {
            "type": log_type,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            log_entry.update(extra)
        self._logs.append(log_entry)
        # 实时保存到数据库
        if self._log_store_callback:
            try:
                self._log_store_callback(
                    self.task_id, self.agent_name, self.task_description,
                    "running", None, None, self._logs, self.get_summary()
                )
            except Exception as e:
                logger.warning(f"[AsyncSubAgentExecutor] 实时保存日志失败: {e}")

    def get_logs(self) -> List[Dict]:
        """获取所有日志"""
        return self._logs.copy()

    def get_summary(self) -> dict:
        """获取任务摘要"""
        elapsed = int(time.time() - self._start_time) if self._start_time else 0
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "task_description": self.task_description,
            "tool_call_count": self._tool_call_count,
            "elapsed_seconds": elapsed,
            "result": self._last_result,
            "error": self._execution_error,
        }

    def start(self):
        """启动执行线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def join(self, timeout: float = None):
        """等待执行完成"""
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        """在线程中执行任务"""
        self._start_time = time.time()
        self._tool_call_count = 0

        try:
            agent = self.agent_manager.get_agent(self.agent_name)
            if not agent:
                self._emit_error(f"Agent not found: {self.agent_name}")
                return

            # 获取系统提示和工具
            system_prompt = self.agent_manager.get_agent_system_prompt(
                self.agent_name, is_subagent_call=self.is_subagent_call
            )
            tools = self.agent_manager.get_agent_tools_schema(self.agent_name)

            messages = [{"role": "system", "content": system_prompt}]

            # 如果 agent 配置了 inherit_history，获取主智能体历史消息
            history_section = ""
            if agent.inherit_history and self._get_history_messages:
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        if agent.inherit_history_count is not None:
                            history_messages = history_messages[-agent.inherit_history_count:]

                        history_lines = []
                        max_chars = getattr(agent, 'inherit_history_max_chars', 500) or 500
                        for msg in history_messages:
                            role = msg.get("role", "user")
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = "".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
                            if content:
                                truncated = content[:max_chars] + ("..." if len(content) > max_chars else "")
                                history_lines.append(f"**{role}**: {truncated}")

                        if history_lines:
                            history_section = "\n\n## 主智能体历史上下文\n" + "\n".join(history_lines)
                except Exception as e:
                    logger.warning(f"[AsyncSubAgentExecutor] 获取历史消息失败: {e}")

            # 构建任务消息
            if self.parent_context:
                content = f"## 父任务上下文\n{self.parent_context}\n\n## 子任务\n{self.task_description}"
            else:
                content = f"## 子任务\n{self.task_description}"

            if history_section:
                content += history_section

            messages.append({"role": "user", "content": content})

            self._add_log("progress", f"开始执行子任务: {self.agent_name}")
            self._emit_progress(f"开始执行子任务: {self.agent_name}")

            try:
                result = self._execute_agent_loop(messages, tools)
            except Exception as e:
                logger.error(f"[AsyncSubAgentExecutor] _execute_agent_loop error: {e}")
                result = f"执行出错: {str(e)}"

            if self._is_cancelled:
                return

            try:
                summary = self._summarize_result(result)
                self._last_result = summary
            except Exception as e:
                logger.error(f"[AsyncSubAgentExecutor] _summarize_result error: {e}")
                summary = result if result else "执行出错"
                self._last_result = summary

            # 任务完成，保存最终状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(
                        self.task_id, self.agent_name, self.task_description,
                        "finished", self._last_result, None, self._logs, self.get_summary()
                    )
                except Exception as e:
                    logger.warning(f"[AsyncSubAgentExecutor] 保存完成状态失败: {e}")

            self._emit_finished(self._last_result)

        except Exception as e:
            logger.error(f"[AsyncSubAgentExecutor] _run() error: {e}")
            self._execution_error = str(e)
            if self._log_store_callback:
                try:
                    self._log_store_callback(
                        self.task_id, self.agent_name, self.task_description,
                        "error", None, self._execution_error, self._logs, self.get_summary()
                    )
                except Exception as e:
                    logger.warning(f"[AsyncSubAgentExecutor] 保存错误状态失败: {e}")
            self._emit_error(f"SubAgent execution error: {str(e)}")

    def _summarize_result(self, result: str) -> str:
        """总结结果"""
        if not result:
            return "任务执行完成，无返回结果"

        # 限制结果长度
        max_length = 5000
        if len(result) > max_length:
            return result[:max_length] + "\n\n[结果已截断]"

        return result

    def _execute_agent_loop(self, messages: List[Dict], tools: List[Dict]) -> str:
        """执行子智能体对话循环"""
        current_messages = messages.copy()
        response_content = ""
        current_reasoning = ""

        while not self._is_cancelled:
            response_content, tool_calls, reasoning_content = self._make_api_call(
                current_messages, tools
            )
            current_reasoning = reasoning_content

            if self._is_cancelled:
                return ""

            if not tool_calls:
                return self._filter_thinking_content(response_content)

            # 构建 assistant 消息
            assistant_msg = {
                "role": "assistant",
                "content": response_content,
                "tool_calls": tool_calls,
            }
            if current_reasoning:
                assistant_msg["reasoning_content"] = current_reasoning
            current_messages.append(assistant_msg)

            tool_results = self._execute_tools(tool_calls)

            if tool_results is None:
                # 等待用户输入
                while self._pending_answer is None and not self._is_cancelled:
                    time.sleep(0.1)

                if self._is_cancelled:
                    return ""

                with self._pending_answer_lock:
                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": self._question_pending["tool_call_id"],
                        "content": self._pending_answer,
                    })
                    self._pending_answer = None
                continue

            current_messages.extend(tool_results)
            time.sleep(0.2)

        return self._filter_thinking_content(response_content)

    def _filter_thinking_content(self, content: str) -> str:
        """过滤掉思考内容，只保留纯回复"""
        if not content:
            return content
        pattern = r"<think>[\s\S]*?</think>"
        return re.sub(pattern, "", content)

    def _parse_tool_arguments_json(self, raw_arguments: Any):
        if isinstance(raw_arguments, dict):
            return raw_arguments, ""

        text = str(raw_arguments or "")
        if not text.strip():
            return {}, ""

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, str(exc)

        if not isinstance(parsed, dict):
            return None, f"expected JSON object, got {type(parsed).__name__}"

        return parsed, ""

    def _make_api_call(self, messages: List[Dict], tools: List[Dict] = None) -> tuple:
        """调用 LLM API"""
        api_key = self.llm_config.get("API_KEY", "").strip()
        base_url = self.llm_config.get("API_URL") or None
        model = str(self.llm_config.get("模型名称", "gpt-4o"))

        req_kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        extra_body = {}
        mapping = {
            "温度": "temperature",
            "最大Token": "max_tokens",
            "核采样": "top_p",
        }

        for cn_key, value in self.llm_config.items():
            if cn_key in ["API_KEY", "API_URL", "模型名称", "系统提示", "启用技能"]:
                continue

            en_key = mapping.get(cn_key)
            if not en_key and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", cn_key):
                en_key = cn_key
            if not en_key:
                continue
            elif en_key in ["temperature", "max_tokens", "top_p"]:
                req_kwargs[en_key] = value
            else:
                extra_body[en_key] = value

        if "max_tokens" in req_kwargs:
            req_kwargs["max_tokens"] = self._cap_max_output_tokens(
                model, req_kwargs["max_tokens"]
            )

        if extra_body:
            req_kwargs["extra_body"] = extra_body

        client = OpenAI(
            api_key=api_key if api_key else "dummy",
            base_url=base_url,
            timeout=120.0,
        )

        def create_completion():
            return client.chat.completions.create(**req_kwargs, tools=tools)

        response = create_api_call_with_retry(client, create_completion)

        full_response = ""
        reasoning_content = ""
        tool_calls_found = []
        tool_calls_buffer = {}

        for chunk in response:
            if self._is_cancelled:
                return "", [], ""

            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                content = self._filter_thinking_content(content)
                full_response += content

            # DeepSeek V4 thinking mode
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_content += reasoning_delta

            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    tc_id = tc.id
                    if tc_id is None:
                        if tool_calls_buffer:
                            tc_id = list(tool_calls_buffer.keys())[-1]
                        else:
                            continue

                    if tc_id not in tool_calls_buffer:
                        tool_calls_buffer[tc_id] = {
                            "id": tc_id,
                            "type": getattr(tc, "type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }

                    buffer = tool_calls_buffer[tc_id]
                    if tc.function and tc.function.name:
                        buffer["function"]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        buffer["function"]["arguments"] += tc.function.arguments

                    if buffer["function"]["name"] and buffer["function"]["arguments"]:
                        parsed_args, _ = self._parse_tool_arguments_json(
                            buffer["function"]["arguments"]
                        )
                        if parsed_args is not None:
                            tool_calls_found.append({
                                "id": buffer["id"],
                                "type": buffer["type"],
                                "function": {
                                    "name": buffer["function"]["name"],
                                    "arguments": json.dumps(parsed_args, ensure_ascii=False),
                                },
                            })
                            del tool_calls_buffer[tc_id]

        # 处理剩余的 buffer
        for tc_id, buffer in list(tool_calls_buffer.items()):
            if not (buffer["function"]["name"] and buffer["function"]["arguments"]):
                continue
            parsed_args, error = self._parse_tool_arguments_json(
                buffer["function"]["arguments"]
            )
            if parsed_args is None:
                logger.warning(
                    f"[AsyncSubAgentExecutor] Dropping invalid tool arguments for "
                    f"tool_call {tc_id} ({buffer['function']['name']}): {error}"
                )
                continue
            tool_calls_found.append({
                "id": buffer["id"],
                "type": buffer["type"],
                "function": {
                    "name": buffer["function"]["name"],
                    "arguments": json.dumps(parsed_args, ensure_ascii=False),
                },
            })

        return full_response, tool_calls_found, reasoning_content

    def _cap_max_output_tokens(self, model: str, requested: int) -> int:
        try:
            requested_int = int(requested)
        except Exception:
            return requested
        profile = get_provider_profile(self.llm_config)
        cap = int(profile.get("max_output_tokens", requested_int))
        if profile.get("family") == "openai":
            model_name = (model or "").lower()
            if "o1" in model_name or "o3" in model_name:
                cap = max(cap, min(requested_int, 32768))
        return min(requested_int, cap)

    def _execute_tools(self, tool_calls: List[Dict]) -> Optional[List[Dict]]:
        """执行工具调用"""
        if not tool_calls or not self.tool_executor:
            return []

        results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except:
                    arguments = {}

            tool_call_id = tc["id"]

            # 处理 question 工具
            if tool_name == "question":
                question_text = arguments.get("question", "")
                options = arguments.get("options", [])
                multiple = arguments.get("multiple", False)
                self._question_pending = {
                    "tool_call_id": tool_call_id,
                    "question": question_text,
                    "options": options,
                    "multiple": multiple,
                }
                return None

            self._tool_call_count += 1
            self._add_log("tool_call", tool_name, {"args": arguments})
            self._emit_tool_call_started(tool_name, arguments)

            result = self.tool_executor.execute(tool_name, arguments)
            result_content = str(result) if result else ""
            success = getattr(result, "success", True) if result else False

            self._add_log("tool_result", tool_name, {"result": result_content, "success": success})
            self._emit_tool_result(tool_name, result_content, success)

            results.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_content,
            })

        return results
