# -*- coding: utf-8 -*-
"""
Chat Worker - OpenAI 对话执行器

使用标准线程替代 QThread，支持前后端分离
"""

import json
import re
import time
import httpcore
import httpx
import threading

from loguru import logger
from collections import deque
from datetime import datetime
from threading import Event
from typing import Any, Dict, List, Callable, Optional, Tuple

from openai import (
    OpenAI, BadRequestError, RateLimitError, APIError, APIConnectionError,
)

from app.core.memory_manager import MEMORY_CATEGORIES
from app.core.provider_profile import get_provider_profile
from app.core.message_content import consolidate_messages, append_text_block, messages_to_api, to_api_message
from app.core.event_bus import Signal, get_event_bus, ChatEvents


# ========== 预编译正则表达式 ==========
_RE_PATH = re.compile(r'"path"\s*:\s*"([^"]*)"')
_RE_FILE_PATH = re.compile(r'"filePath"\s*:\s*"([^"]*)"')
_RE_COMMAND = re.compile(r'"command"\s*:\s*"([^"]*)"')
_RE_CONTENT_KEY = re.compile(r'"content"\s*:\s*"')
_RE_ARG_PATTERN = {
    "url": re.compile(r'"url"\s*:\s*"([^"]*)"'),
    "pattern": re.compile(r'"pattern"\s*:\s*"([^"]*)"'),
    "query": re.compile(r'"query"\s*:\s*"([^"]*)"'),
    "name": re.compile(r'"name"\s*:\s*"([^"]*)"'),
    "question": re.compile(r'"question"\s*:\s*"([^"]*)"'),
}
_RE_GENERIC_ARG = re.compile(r'"({param})"\s*:\s*"([^"]*)"')
# ========== END ==========


def _try_fix_malformed_json_arguments(raw_args: str, tool_name: str) -> Tuple[Optional[Dict], str]:
    """尝试修复模型生成的不规范 JSON"""
    if not raw_args or not isinstance(raw_args, str):
        return None, "empty_or_invalid_input"
    
    args = {}
    
    path_match = _RE_PATH.search(raw_args)
    if path_match:
        args["path"] = path_match.group(1)
    
    if "filePath" not in args:
        file_path_match = _RE_FILE_PATH.search(raw_args)
        if file_path_match:
            args["filePath"] = file_path_match.group(1)
    
    command_match = _RE_COMMAND.search(raw_args)
    if command_match:
        args["command"] = command_match.group(1)
    
    for param_name, pattern in _RE_ARG_PATTERN.items():
        matches = pattern.finditer(raw_args)
        for match in matches:
            args[param_name] = match.group(1)
    
    is_write_format = '"path"' in raw_args and '"content"' in raw_args
    is_content_only = raw_args.strip().startswith('"content"') or '"content"' in raw_args
    
    if is_write_format or is_content_only:
        content_key_match = _RE_CONTENT_KEY.search(raw_args)
        if content_key_match:
            content_start = content_key_match.end()
            last_brace = raw_args.rfind('}')
            last_bracket = raw_args.rfind(']')
            json_end = max(last_brace, last_bracket) if last_bracket > 0 else last_brace
            
            if json_end > content_start:
                content_value = raw_args[content_start:json_end]
                content_value = content_value.rstrip('"').rstrip()
                
                if content_value:
                    import copy
                    try:
                        test_json = copy.deepcopy(args)
                        test_json["content"] = content_value
                        json.dumps(test_json, ensure_ascii=False)
                        args["content"] = content_value
                        return args, "fixed_content"
                    except Exception:
                        pass
                
                if content_value.endswith(',') or content_value.endswith(';'):
                    extended = content_value.rstrip(',;').rstrip()
                    if extended:
                        args["content"] = extended
                        return args, "fixed_truncated"
                
                args["content"] = content_value
                return args, "fixed_content_only"
    
    if tool_name == "bash":
        if "command" in args:
            return {"command": args["command"]}, "fixed_bash"
    elif args:
        return args, "fixed_partial"
    
    return None, "fix_failed"


def _smart_parse_arguments(raw_args: str, tool_name: str) -> Optional[Dict]:
    """智能解析 arguments"""
    if not raw_args:
        return {}
    
    try:
        return json.loads(raw_args)
    except json.JSONDecodeError:
        pass
    
    fixed_args, status = _try_fix_malformed_json_arguments(raw_args, tool_name)
    if fixed_args:
        logger.info(f"[ToolCall] JSON 智能修复成功: tool={tool_name}, status={status}")
        return fixed_args
    
    return None


class OpenAIChatWorker(threading.Thread):
    """
    OpenAI 对话执行器 - 使用标准线程替代 QThread
    
    信号通过 EventBus 发布，前端可订阅：
    ```python
    bus = get_event_bus()
    bus.subscribe("stream:chunk", on_chunk)
    ```
    
    也支持 Signal 回调：
    ```python
    worker.content_received.connect(handler)
    ```
    """
    
    _DEFERRED_PREVIEW_TOOLS = {"question", "task", "todowrite", "todoread"}
    
    def __init__(
        self,
        messages: List[Dict],
        session_messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict] = None,
        stream: bool = True,
        tool_executor=None,
        tool_start_callback=None,
        get_stage_prompt=None,
        stage_changed_callback=None,
        permission_check_callback=None,
        compaction_prompt: str = "",
        compaction_config: Dict = None,
        event_bus=None,
    ):
        super().__init__(daemon=True)
        
        # 事件总线
        self._event_bus = event_bus or get_event_bus()
        
        # ========== Signal 定义（兼容 PyQt 风格 API）==========
        self.content_received = Signal(str)
        self.reasoning_content_received = Signal(str)  # DeepSeek thinking mode
        self.error_occurred = Signal(str)
        self.finished_with_content = Signal(str)
        self.finished_with_messages = Signal(list)
        self.compaction_status_changed = Signal(dict)
        self.tool_call_started = Signal(str, str, dict, str)
        self.tool_result_received = Signal(str, str, dict, object)
        self.question_asked = Signal(str, str, list, bool)
        self.permission_approval_requested = Signal(str, str, dict)
        
        # 初始化属性
        self.messages = messages
        self.session_messages = consolidate_messages(session_messages or [])
        self.llm_config = llm_config
        self.tools = tools or []
        self.stream = stream
        self.tool_executor = tool_executor
        self.tool_start_callback = tool_start_callback
        self.get_stage_prompt = get_stage_prompt
        self.stage_changed_callback = stage_changed_callback
        self.permission_check_callback = permission_check_callback
        self.compaction_prompt = compaction_prompt
        self.compaction_config = compaction_config or {}
        
        # 初始化工作线程属性
        self.full_response = ""
        self.tools = tools or []
        self.stream = stream
        self.tool_executor = tool_executor
        self.tool_start_callback = tool_start_callback
        self.get_stage_prompt = get_stage_prompt
        self.stage_changed_callback = stage_changed_callback
        self.permission_check_callback = permission_check_callback
        self.compaction_prompt = compaction_prompt
        self.compaction_config = compaction_config or {}
        self.full_response = ""
        self._response_chunks: deque = deque()  # 性能优化：deque 比 list 的 append 更快
        self._is_cancelled = False
        self._question_pending = None
        self._pending_answer = None
        self._answer_event = Event()
        self._permission_pending = None
        self._permission_approved = False
        self._round_permission_cache = {}  # 该轮对话内自动允许
        self._session_permission_cache = {}  # 本次会话内自动允许
        self._previewed_tool_call_ids = set()
        self._current_tool_calls = {}  # 改成字典
        self._tool_calls_buffer = {}
        self._reasoning_content = ""
        
        # ========== 性能优化：HTTP 客户端和参数缓存 ==========
        self._http_client: Optional[Any] = None  # 复用的 HTTP 客户端
        self._cached_api_config: Optional[Dict[str, Any]] = None  # 缓存的 API 配置
        self._reasoning_chunks: deque = deque()  # 性能优化：deque 比 list 的 append 更快
        self._response_content_blocks = []
        self._tool_execution_cancelled = False
        # 等待完整参数的 tool_calls（用于处理超长 arguments 流式传输场景）
        self._waiting_tool_params: Dict[str, dict] = {}  # tool_call_id -> {buffer, attempt_count}
        self._max_param_retry_count = 10  # 最多重试10次（每收到一个 chunk 重试一次）
        self._last_compaction_state = {
            "active": False,
            "source": "worker",
            "kind": "",
            "original_count": len(messages or []),
            "summarized_count": 0,
            "kept_count": len(messages or []),
            "summary_count": 0,
            "note": "",
        }
        self._current_session_messages = list(self.session_messages)
        
        # ========== 性能优化：API 消息缓存 ==========
        # 缓存已转换的 API 消息，避免每次 API 调用都重新处理所有消息
        # 只在首次构建，之后增量追加
        self._api_messages_cache: Optional[List[Dict[str, Any]]] = None
        self._api_messages_built = False  # 是否已完成初始构建
        
        # API 模式专用：直接回调（绕过 Qt 信号-槽）
        self._direct_callbacks: Dict[str, Callable] = {}
    
    def _build_api_messages_cache(self) -> List[Dict[str, Any]]:
        """
        构建 API 消息缓存。
        只在首次调用时处理所有消息，之后增量追加。
        
        Returns:
            转换后的 API 消息列表
        """
        if self._api_messages_cache is not None:
            return self._api_messages_cache
        
        # 首次构建：处理所有历史消息
        self._api_messages_cache = messages_to_api(self.messages)
        self._api_messages_built = True
        return self._api_messages_cache
    
    def _append_to_api_cache(self, new_messages: List[Dict[str, Any]]) -> None:
        """
        将新消息追加到 API 缓存。
        只转换新消息并追加，避免重新处理整个列表。
        
        Args:
            new_messages: 新增的消息列表
        """
        if self._api_messages_cache is None:
            self._api_messages_cache = messages_to_api(new_messages)
            return
        
        # 只转换新消息并追加
        for msg in new_messages:
            api_msg = to_api_message(msg)
            if api_msg:
                if api_msg.get("role") == "user" and not api_msg.get("content"):
                    continue
                self._api_messages_cache.append(api_msg)

    def set_direct_callbacks(self, callbacks: Dict[str, Callable]) -> None:
        """设置直接回调（API 模式专用，绕过 Qt 信号-槽）
        
        Args:
            callbacks: 回调字典，键为信号名，值为回调函数
        """
        self._direct_callbacks = callbacks

    def _emit_direct(self, signal_name: str, *args) -> None:
        """直接调用回调（API 模式，替代 Qt 信号发射）
        
        Args:
            signal_name: 信号名
            *args: 传递给回调的参数
        """
        callback = self._direct_callbacks.get(signal_name)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                from loguru import logger
                logger.error(f"[Worker] Direct callback error for {signal_name}: {e}")

    def _emit_with_callback(self, signal_name: str, signal, *args) -> None:
        """发射信号并尝试直接回调（API 模式优先使用直接回调）
        
        Args:
            signal_name: 信号名（用于查找直接回调）
            signal: Qt 信号对象
            *args: 传递给回调/信号的参数
        """
        # API 模式：优先使用直接回调（避免 Qt 信号跨线程问题）
        if self._direct_callbacks:
            self._emit_direct(signal_name, *args)
        # UI 模式：发射 Qt 信号
        if signal is not None:
            signal.emit(*args)

    def cancel(self):
        self._is_cancelled = True
        self._tool_execution_cancelled = True
        self._answer_event.set()
        if self._question_pending:
            self._question_pending = None
        if self._permission_pending:
            self._permission_pending = None

    def get_interrupted_messages(self) -> List[Dict]:
        # 性能优化：使用 extend 代替 + 操作
        snapshot = list(self._current_session_messages or self.session_messages or [])
        partial_sequence = self._build_response_message_sequence()
        if partial_sequence:
            snapshot.extend(partial_sequence)
        return consolidate_messages(snapshot)

    def _clear_pending_response_state(self):
        """
        清理单轮对话结束后的中间状态。
        在每次 API 调用前和工具执行完成后调用，释放内存。
        """
        # 清理工具调用相关的缓存
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        self._waiting_tool_params = {}
        self._previewed_tool_call_ids = set()
        
        # 清理本轮响应内容（但保留 full_response 用于最终输出）
        self._response_content_blocks = []
        
        # 清理本轮的 reasoning_content（下一轮会有新的）
        self._reasoning_content = ""
        self._reasoning_chunks = []  # 清理思考片段缓存
        
        # 注意：_round_permission_cache 不在这里清理，改到工具执行完成后清理
    
    def _get_reasoning_content(self) -> str:
        """获取当前的 reasoning_content（从累积的 chunks 合成）"""
        if self._reasoning_chunks:
            return ''.join(self._reasoning_chunks)
        return self._reasoning_content

    def cleanup(self):
        """
        彻底清理 worker 的所有缓存数据，防止内存泄漏。
        应该在对话结束后调用。
        """
        import sys
        from loguru import logger
        
        # 计算清理前的内存占用估算
        msg_count = len(self.messages) + len(self.session_messages) + len(self._current_session_messages or [])
        full_resp_len = len(self.full_response or "")
        reasoning_len = len(self._reasoning_content or "")
        blocks_count = len(self._response_content_blocks or [])
        
        # 记录清理的概况
        if full_resp_len > 100000 or reasoning_len > 100000:
            logger.info(f"[Worker] 清理大量缓存: full_response={full_resp_len/1024:.1f}KB, "
                       f"reasoning={reasoning_len/1024:.1f}KB, messages={msg_count}, blocks={blocks_count}")
        
        # 清理消息引用
        self.messages = []
        self.session_messages = []
        self._current_session_messages = []
        
        # 清理响应缓存
        self.full_response = ""
        self._reasoning_content = ""
        self._reasoning_chunks = []  # 性能优化：清理思考片段缓存
        self._response_content_blocks = []
        self._response_chunks = []  # 性能优化：清理响应片段缓存
        
        # 清理工具调用缓存
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        self._waiting_tool_params = {}
        self._previewed_tool_call_ids = set()
        
        # 清理 API 消息缓存
        self._api_messages_cache = None
        self._api_messages_built = False
        
        # 清理工具列表引用
        self.tools = []
        
        # 清理回调
        self._direct_callbacks = {}
        
        # 清理问题/回答状态
        self._pending_answer = None
        self._question_pending = None
        
        # 清理会话缓存
        self._session_messages = []
        
        # 清理 HTTP 客户端缓存
        self._http_client = None
        self._cached_api_config = None
    
    def _get_http_client(self) -> Any:
        """
        获取或创建复用的 HTTP 客户端。
        避免每次 API 调用都创建新的客户端。
        """
        if self._http_client is None:
            self._http_client = OpenAI(
                api_key=self.llm_config.get("API_KEY", "").strip(),
                base_url=self.llm_config.get("API_URL"),
                timeout=httpx.Timeout(600.0, connect=60.0),
            )
        return self._http_client
    
    def _build_api_request_kwargs(self) -> Dict[str, Any]:
        """
        预构建 API 请求参数，避免每次调用都重复处理。
        缓存结果，只在配置变化时重新构建。
        """
        # 检查是否需要更新缓存
        config_key = str(self.llm_config.get("API_KEY", "")) + str(self.llm_config.get("API_URL", ""))
        
        if self._cached_api_config is not None and self._cached_api_config.get("_config_key") == config_key:
            # 缓存有效，返回基础配置（messages 和 tools 每次不同，需要单独设置）
            return {
                "model": self._cached_api_config["model"],
                "stream": self.stream,
                "extra_body": dict(self._cached_api_config.get("extra_body") or {}),
                "_auth_headers": self._cached_api_config.get("_auth_headers"),
                "_is_o1_model": self._cached_api_config.get("_is_o1_model"),
            }
        
        # 构建新的缓存
        api_key = self.llm_config.get("API_KEY", "").strip()
        base_url = self.llm_config.get("API_URL") or None
        model = str(self.llm_config.get("模型名称", "gpt-4o"))
        
        extra_body = {}
        mapping = {
            "温度": "temperature",
            "最大Token": "max_tokens",
            "核采样": "top_p",
            "频率惩罚": "presence_penalty",
            "重复惩罚": "frequency_penalty",
            "思考等级": "reasoning_effort",
        }
        
        skip_params = {"temperature", "top_p", "presence_penalty", "frequency_penalty", "reasoning_effort"}
        if model and (model.startswith("o1") or model.startswith("o3")):
            skip_params.update({"temperature", "top_p"})
        
        for cn_key, value in self.llm_config.items():
            if cn_key in ["API_KEY", "API_URL", "模型名称", "系统提示", "启用技能"]:
                continue
            en_key = mapping.get(cn_key)
            if not en_key and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", cn_key):
                en_key = cn_key
            if not en_key or en_key in skip_params:
                continue
            if en_key in ["max_tokens"]:
                continue  # 单独处理
            extra_body[en_key] = value
        
        # 处理 max_tokens
        max_tokens = self.llm_config.get("最大Token")
        if max_tokens is not None:
            extra_body["max_tokens"] = self._cap_max_output_tokens(model, max_tokens)
        
        # 处理认证
        auth_headers = None
        auth_type = self.llm_config.get("认证方式", "bearer")
        if auth_type == "bce":
            import base64
            auth_str = f"{api_key}:{api_key}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            auth_headers = {"Authorization": f"Basic {b64_auth}"}
        
        is_o1 = model.startswith("o1") or model.startswith("o3")
        
        self._cached_api_config = {
            "_config_key": config_key,
            "model": model,
            "extra_body": extra_body,
            "_auth_headers": auth_headers,
            "_is_o1_model": is_o1,
        }
        
        return {
            "model": model,
            "stream": self.stream,
            "extra_body": extra_body,
            "_auth_headers": auth_headers,
            "_is_o1_model": is_o1,
        }
    
    def provide_answer(self, answer: str):
        self._pending_answer = answer
        self._answer_event.set()

    def set_session_permission_cache(self, tool_name: str, allowed: bool = True):
        """设置会话级权限缓存（本次会话允许）"""
        if allowed:
            self._session_permission_cache[tool_name] = True
        elif tool_name in self._session_permission_cache:
            del self._session_permission_cache[tool_name]

    def approve_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        from loguru import logger
        if (
            self._permission_pending
            and self._permission_pending.get("tool_call_id") == tool_call_id
        ):
            tool_name = self._permission_pending.get("tool_name", "")
            if auto_allow:
                self._round_permission_cache[tool_name] = True
                logger.info(f"[Permission] 设置轮缓存: tool={tool_name}")
            if session_allow:
                self._session_permission_cache[tool_name] = True
                logger.info(f"[Permission] 设置会话缓存: tool={tool_name}")
            self._permission_approved = True
            self._permission_pending = None

    def deny_permission(self, tool_call_id: str):
        if (
            self._permission_pending
            and self._permission_pending.get("tool_call_id") == tool_call_id
        ):
            self._permission_approved = False
            self._permission_pending = None

    def run(self):
        try:
            current_messages = self.messages.copy()
            current_session_messages = list(self.session_messages)
            self._current_session_messages = list(current_session_messages)
            self._emit_compaction_status(self._last_compaction_state)
            self.full_response = ""
            self._reasoning_content = ""
            # 开始新对话时，清理所有中间状态，重建 API 消息缓存
            self._clear_pending_response_state()
            self._api_messages_cache = None  # 重置缓存，下次 API 调用时重建
            self._api_messages_built = False

            # 开始新对话时，清理该轮缓存，但保留会话级缓存
            self._round_permission_cache = {}

            while not self._is_cancelled:
                if self._is_cancelled:
                    return
                
                # 每次 API 调用前，清理上一轮的中间状态
                self._clear_pending_response_state()
                
                # 使用 API 消息缓存（首次会重建，后续复用）
                tool_calls_found, tool_args_pending = self._make_api_call(current_messages, use_cache=True)
                if tool_calls_found and tool_args_pending:
                    continue
                if self._is_cancelled:
                    return

                if not tool_calls_found:
                    response_sequence = self._build_response_message_sequence()
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存：追加响应消息
                    self._append_to_api_cache(response_sequence)
                    # 性能优化：在发送前才合成完整响应字符串
                    self.full_response = ''.join(self._response_chunks)
                    self._emit_with_callback("finished_with_messages", self.finished_with_messages, current_session_messages)
                    self._clear_pending_response_state()
                    self._emit_with_callback("finished_with_content", self.finished_with_content, self.full_response)
                    return

                tool_results = self._execute_all_tools()

                if tool_results is None:
                    self._answer_event.clear()
                    while self._pending_answer is None and not self._is_cancelled:
                        if self._answer_event.wait(timeout=1.0):
                            break

                    if self._is_cancelled:
                        return

                    q = self._question_pending
                    response_sequence = self._build_response_message_sequence()
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    question_result = {
                        "role": "tool",
                        "tool_call_id": q["tool_call_id"],
                        "content": self._pending_answer,
                    }
                    current_messages.append(question_result)
                    current_session_messages.append(question_result)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存
                    self._append_to_api_cache(response_sequence + [question_result])
                    self._emit_with_callback("finished_with_messages", self.finished_with_messages, current_session_messages)
                    self._question_pending = None
                    self._pending_answer = None
                    self._answer_event.clear()
                    continue
                response_sequence = self._build_response_message_sequence(tool_results)
                current_messages.extend(response_sequence)
                current_session_messages.extend(response_sequence)
                self._current_session_messages = list(current_session_messages)
                # 更新 API 消息缓存
                self._append_to_api_cache(response_sequence)
                self._emit_with_callback("finished_with_messages", self.finished_with_messages, current_session_messages)

                self._check_and_notify_stage_change()

                QCoreApplication.processEvents()
                time.sleep(0.01)

        except Exception as e:
            logger.exception("请求失败!")
            self._handle_error(e)
        finally:
            # 工具执行完成后，清理该轮权限缓存（为下一轮 API 调用做准备）
            self._round_permission_cache = {}

    def _build_response_message_sequence(self, tool_results=None) -> List[Dict]:
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 性能优化：缓存 reasoning_content，避免重复 join
        reasoning_content = self._get_reasoning_content()
        
        tool_call_map = {}
        # 从 _current_tool_calls 字典获取 tool_calls
        current_tcs = self._current_tool_calls
        if current_tcs:
            for tc in current_tcs.values():
                if not isinstance(tc, dict):
                    continue
                tool_call_id = str(tc.get("id") or "")
                if not tool_call_id:
                    continue
                function = tc.get("function") or {}
                normalized_tc = {
                    "id": tool_call_id,
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": function.get("name"),
                        "arguments": function.get("arguments", "{}"),
                    },
                }
                tool_call_map[tool_call_id] = normalized_tc
        
        # 从 _tool_calls_buffer 获取未解析完成的 tool_calls
        tool_calls_buffer = self._tool_calls_buffer
        if tool_calls_buffer:
            for tc_id, buffer in tool_calls_buffer.items():
                if tc_id in tool_call_map:
                    continue
                function = buffer.get("function") or {}
                tool_call_map[tc_id] = {
                    "id": tc_id,
                    "type": buffer.get("type", "function"),
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    },
                }
        
        # 也从 tool_results 中的 _tool_call 字段获取
        if tool_results:
            for item in tool_results:
                if isinstance(item, dict) and "tool_call_id" in item:
                    tc_id = item["tool_call_id"]
                    if tc_id and tc_id not in tool_call_map:
                        tool_call_map[tc_id] = {
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }

        tool_result_map = {}
        MAX_CONTENT_LENGTH = 6000
        if tool_results:
            for item in tool_results:
                if not isinstance(item, dict):
                    continue
                tool_call_id = str(item.get("tool_call_id") or "")
                if not tool_call_id:
                    continue
                
                content = item.get("content", "")
                if len(content) > MAX_CONTENT_LENGTH:
                    head_len = int(MAX_CONTENT_LENGTH * 0.6)
                    tail_len = MAX_CONTENT_LENGTH - head_len
                    content = (
                        content[:head_len]
                        + f"\n\n... [已截断，原始长度 {len(content)}] ...\n\n"
                        + content[-tail_len:]
                    )
                
                tool_result_map[tool_call_id] = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": item.get("name", "tool"),
                    "arguments": item.get("arguments", {}),
                    "content": content,
                    "success": item.get("success", True),
                    "round_id": item.get("round_id"),
                    "timestamp": item.get("timestamp", now_ts),
                }

        # 预防性修复：过滤掉没有对应 tool 结果的 tool_call
        # 只有有结果的 tool_call 才能发送给 API，避免用户中断时产生 2013 错误
        if tool_result_map:
            valid_tc_ids = set(tool_result_map.keys())
            filtered_tool_call_map = {}
            for tc_id, tc in tool_call_map.items():
                if tc_id in valid_tc_ids:
                    filtered_tool_call_map[tc_id] = tc
                else:
                    logger.warning(f"[ToolCall预防] 过滤了无结果的 tool_call: {tc_id[:20]}...")
            tool_call_map = filtered_tool_call_map

        sequence: List[Dict] = []
        pending_text_blocks = []
        response_blocks = self._response_content_blocks
        if response_blocks:
            for block in response_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text", ""))
                    if text:
                        pending_text_blocks = append_text_block(pending_text_blocks, text)
                    continue

                if block_type != "tool_call_marker":
                    continue

                tool_call_id = str(block.get("tool_call_id") or "")
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "timestamp": now_ts,
                }
                if pending_text_blocks:
                    assistant_msg["content"] = pending_text_blocks[0].get("text")
                tool_call = tool_call_map.get(tool_call_id)
                if tool_call:
                    assistant_msg["tool_calls"] = [tool_call]
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
                    sequence.append(assistant_msg)

                pending_text_blocks = []
                tool_result = tool_result_map.get(tool_call_id)
                if tool_result:
                    sequence.append(tool_result)

        # 处理没有对应 marker 的 tool_result
        for tool_call_id, tool_result in tool_result_map.items():
            already_added = False
            for block in sequence:
                if block.get("role") == "tool" and block.get("tool_call_id") == tool_call_id:
                    already_added = True
                    break
            if not already_added:
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "timestamp": now_ts,
                }
                tool_call = tool_call_map.get(tool_call_id)
                if tool_call:
                    assistant_msg["tool_calls"] = [tool_call]
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                if assistant_msg.get("tool_calls"):
                    sequence.append(assistant_msg)
                sequence.append(tool_result)

        if pending_text_blocks:
            assistant_msg = {
                "role": "assistant",
                "content": pending_text_blocks[0].get("text"),
                "timestamp": now_ts,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            sequence.append(assistant_msg)
        elif not sequence and self.full_response:
            assistant_msg = {
                "role": "assistant",
                "content": append_text_block([], self.full_response)[0].get("text"),
                "timestamp": now_ts,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            sequence.append(assistant_msg)
        elif not sequence and not response_blocks:
            sequence.append({"role": "assistant", "content": [], "timestamp": now_ts})

        return sequence

    def _emit_compaction_status(self, state: Dict):
        # 性能优化：减少重复的 dict.get 调用
        state = state or {}
        normalized = {
            "active": bool(state.get("active", False)),
            "source": state.get("source", "worker"),
            "kind": state.get("kind", ""),
            "original_count": int(state.get("original_count", 0) or 0),
            "summarized_count": int(state.get("summarized_count", 0) or 0),
            "kept_count": int(state.get("kept_count", 0) or 0),
            "summary_count": int(state.get("summary_count", 0) or 0),
            "note": str(state.get("note", "") or ""),
        }
        if normalized == self._last_compaction_state:
            return
        self._last_compaction_state = normalized
        self._emit_with_callback("compaction_status_changed", self.compaction_status_changed, dict(normalized))

    def _fix_tool_result_order(self, messages: List[Dict]) -> tuple[List[Dict], bool]:
        """
        修复消息列表中 tool result 顺序问题。
        
        处理 API 格式消息（tool 消息只有 role, tool_call_id, name, content）。
        规则：每个 tool 消息的 tool_call_id 必须与之前的 assistant 消息的 tool_calls 中的 id 匹配。
        
        主要问题：
        1. 用户中断时：assistant 消息已包含 tool_calls，但对应的 tool 结果还没有被追加
        2. 重复的 tool_call_id：之前的修复尝试可能累积了重复的 tool_call_id
        
        修复策略：
        1. 收集所有 tool 消息的 tool_call_id（这些是"有效的"）
        2. 对每个 assistant 消息，只保留那些在 tool 消息中存在对应结果的 tool_call
        3. 如果所有 tool_call 都没有对应结果（用户中断场景），移除 tool_calls 字段
        4. 如果有重复的 tool_call_id，只保留第一个
        
        Returns:
            (修复后的消息列表, 是否进行了修复)
        """
        fixed_messages: List[Dict] = []
        modified = False
        
        # 第一步：收集所有 tool 消息中的 tool_call_id（这些是"有效的"）
        valid_tool_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id:
                    valid_tool_call_ids.add(tc_id)
        
        logger.warning(f"[ToolCall修复] 有效 tool_call_ids: {len(valid_tool_call_ids)} 个")
        
        # 如果没有任何 tool 消息，说明是用户中断场景，但没有累积的工具结果
        # 这种情况下直接返回无需修复
        if not valid_tool_call_ids:
            # 检查是否有 assistant 消息包含 tool_calls
            has_tool_calls = any(
                msg.get("role") == "assistant" and msg.get("tool_calls")
                for msg in messages
            )
            if has_tool_calls:
                logger.warning("[ToolCall修复] 检测到用户中断场景：assistant 有 tool_calls 但无任何 tool 结果")
                # 移除所有 assistant 消息中的 tool_calls
                for msg in messages:
                    if msg.get("role") == "assistant":
                        if msg.get("tool_calls"):
                            msg["tool_calls"] = []
                            modified = True
                            logger.info("[ToolCall修复] 已移除中断时的 tool_calls")
                return messages, modified
            return messages, False
        
        # 第二步：遍历每个 assistant 消息，修复 tool_calls
        for msg in messages:
            if msg.get("role") != "assistant":
                fixed_messages.append(msg)
                continue
            
            # 深拷贝，避免修改原消息
            fixed_msg = dict(msg)
            tool_calls = fixed_msg.get("tool_calls") or []
            
            if not tool_calls:
                fixed_messages.append(fixed_msg)
                continue
            
            # 去重并过滤：只保留有对应 tool 结果的 tool_call
            seen_ids: set = set()
            new_tool_calls: List[Dict] = []
            removed_count = 0
            
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                
                # 检查重复
                if tc_id in seen_ids:
                    logger.warning(f"[ToolCall修复] 发现重复 tool_call_id: {tc_id[:20]}...，已移除")
                    modified = True
                    removed_count += 1
                    continue
                
                # 检查是否有对应的 tool 结果
                if tc_id and tc_id not in valid_tool_call_ids:
                    logger.warning(f"[ToolCall修复] tool_call {tc_id[:20]}... 无对应 tool 结果，已移除")
                    modified = True
                    removed_count += 1
                    continue
                
                seen_ids.add(tc_id)
                new_tool_calls.append(tc)
            
            if new_tool_calls:
                fixed_msg["tool_calls"] = new_tool_calls
            else:
                # 所有 tool_call 都没有对应结果，移除 tool_calls 字段
                fixed_msg.pop("tool_calls", None)
                logger.info("[ToolCall修复] 所有 tool_call 均无对应结果，已移除 tool_calls 字段")
            
            fixed_messages.append(fixed_msg)
        
        return fixed_messages, modified

    def _try_recover_tool_arguments(self, messages: List[Dict]) -> Optional[List[Dict]]:
        """
        尝试从历史消息中恢复 tool_calls 的参数。
        
        当检测到 "Missing required arguments" 错误时调用。
        检查是否有 tool 结果被错误处理导致参数丢失。
        
        Returns:
            修复后的消息列表，如果无法修复则返回 None
        """
        try:
            # 查找所有 assistant 消息中的 tool_calls
            tool_calls_by_content_hash: Dict[str, Dict] = {}
            
            for msg in messages:
                if msg.get("role") == "assistant":
                    tool_calls = msg.get("tool_calls") or []
                    for tc in tool_calls:
                        function = tc.get("function", {}) or {}
                        arguments = function.get("arguments", "{}")
                        # 使用 arguments 的 hash 作为键（用于匹配）
                        args_hash = str(arguments)[:100]
                        if args_hash:
                            tool_calls_by_content_hash[args_hash] = tc
            
            # 检查是否有 tool 消息缺少必要的参数信息
            # 如果有对应的 assistant 消息中有完整的 tool_calls，说明参数可能被错误处理了
            if not tool_calls_by_content_hash:
                logger.warning("[ToolCall恢复] 未找到任何 tool_calls，无法恢复参数")
                return None
            
            # 返回 None 表示无法自动恢复，但记录了尝试
            logger.info(f"[ToolCall恢复] 找到 {len(tool_calls_by_content_hash)} 个 tool_calls 用于参数匹配")
            return None
            
        except Exception as e:
            logger.warning(f"[ToolCall恢复] 尝试恢复工具参数时出错: {e}")
            return None

    def _make_api_call(self, messages: List[Dict], use_cache: bool = True) -> bool:
        """
        发起 API 调用。
        
        性能优化：
        1. 使用缓存的 API 消息，避免每次都重新处理所有消息
        2. 使用缓存的 HTTP 客户端，避免每次都创建新客户端
        3. 预构建 API 参数，避免每次都重复处理
        """
        # 性能优化：使用缓存的 API 消息
        if use_cache and self._api_messages_cache is not None:
            sanitized = self._api_messages_cache
        else:
            sanitized = messages_to_api(messages)
            if use_cache:
                self._api_messages_cache = sanitized
                self._api_messages_built = True
        
        # 性能优化：使用预构建的 API 参数
        cached_config = self._build_api_request_kwargs()
        
        req_kwargs: Dict[str, Any] = {
            "model": cached_config["model"],
            "messages": sanitized,
            "stream": cached_config["stream"],
        }
        
        # 添加 extra_body
        if cached_config.get("extra_body"):
            req_kwargs["extra_body"] = cached_config["extra_body"]
        
        # 添加认证头
        if cached_config.get("_auth_headers"):
            req_kwargs["extra_headers"] = cached_config["_auth_headers"]
        
        # 添加 tools
        if self.tools:
            req_kwargs["tools"] = self.tools
        
        # 处理 o1 模型
        if cached_config.get("_is_o1_model"):
            req_kwargs.pop("stream", None)
            self.stream = False

        # 性能优化：使用复用的 HTTP 客户端
        client = self._get_http_client()

        max_retries = 15
        retry_delay = 5
        last_error = None

        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(**req_kwargs)
                break
            except BadRequestError as e:
                error_str = str(e)
                # 检测 tool call result 错误码 2013
                is_tool_call_order_error = "2013" in error_str or "tool call result does not follow tool call" in error_str.lower()
                
                if is_tool_call_order_error and attempt < max_retries - 1:
                    # 自动修复 tool result 顺序问题
                    logger.warning(f"[API] 检测到 tool call result 顺序错误 (2013)，尝试自动修复...")
                    
                    # 打印最近的消息用于调试
                    recent_msgs = req_kwargs["messages"][-10:] if len(req_kwargs["messages"]) > 10 else req_kwargs["messages"]
                    for i, msg in enumerate(recent_msgs):
                        role = msg.get("role", "?")
                        has_tc = "tool_calls" in msg
                        tc_ids = [tc.get("id", "") for tc in msg.get("tool_calls", [])] if has_tc else []
                        tc_id = msg.get("tool_call_id", "") if role == "tool" else ""

                    fixed_messages, was_fixed = self._fix_tool_result_order(req_kwargs["messages"])
                    
                    if was_fixed:
                        req_kwargs["messages"] = messages_to_api(fixed_messages)
                        logger.warning(f"[API] 已修复消息顺序，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.error(f"[API] 无法自动修复 tool call result 顺序问题 - 可能需要查看上面的消息结构")
                        
                # 检测 Missing required arguments 错误（工具参数丢失）
                is_missing_args_error = "Missing required arguments" in error_str or "missing a required argument" in error_str.lower()
                
                if is_missing_args_error and attempt < max_retries - 1:
                    logger.warning(f"[API] 检测到工具参数丢失错误，尝试从历史消息中恢复...")
                    
                    # 尝试从历史消息中恢复 tool_calls 的参数
                    fixed_messages = self._try_recover_tool_arguments(req_kwargs["messages"])
                    
                    if fixed_messages is not None:
                        req_kwargs["messages"] = messages_to_api(fixed_messages)
                        logger.warning(f"[API] 已恢复工具参数，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.warning(f"[API] 无法恢复工具参数，保持现有消息")
                        
                # 其他 BadRequestError 继续抛出
                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                raise
            except Exception as e:
                last_error = e
                error_str = str(e)
                error_type = type(e).__name__

                # 判断是否应该重试 - 使用异常继承关系系统性覆盖
                # httpx/httpcore 的异常体系：
                # - NetworkError: 连接失败、协议错误等
                # - TimeoutException: 所有超时（Read/Write/Connect）
                # - ProtocolError: 协议层错误（RemoteProtocolError, LocalProtocolError）
                is_retryable_network = isinstance(e, (httpx.NetworkError, httpcore.NetworkError))
                is_retryable_timeout = isinstance(e, (httpx.TimeoutException, httpcore.TimeoutException))
                is_retryable_protocol = isinstance(e, (httpx.ProtocolError, httpcore.ProtocolError))
                is_rate_limit = isinstance(e, RateLimitError)
                is_server_overload = isinstance(e, APIError) and ("2064" in error_str or "overload" in error_str.lower())
                is_conn_error = isinstance(e, APIConnectionError)

                should_retry = (
                    is_rate_limit or is_server_overload or is_conn_error or
                    is_retryable_network or is_retryable_timeout or is_retryable_protocol
                )

                if should_retry and attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)
                    if is_rate_limit:
                        retry_reason = "RateLimit"
                    elif is_server_overload:
                        retry_reason = "ServerOverload"
                    elif is_retryable_timeout:
                        retry_reason = "Timeout"
                    elif is_retryable_protocol:
                        retry_reason = "ProtocolError"
                    else:
                        retry_reason = "ConnectionError"
                    logger.warning(
                        f"[API] {retry_reason} ({error_type}): {error_str[:120]}, "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait_time)
                    continue

                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                raise

        return self._process_response(response)

    def _cap_max_output_tokens(self, model: str, requested: int) -> int:
        try:
            requested_int = int(requested)
        except Exception:
            return requested
        profile = get_provider_profile(self.llm_config)
        cap = int(profile.get("max_output_tokens", requested_int))
        if profile.get("family") == "openai":
            model_name = (model or "").lower()
            if "gpt-4-turbo" in model_name:
                cap = min(cap, 4096)
            elif "o1" in model_name or "o3" in model_name:
                cap = max(cap, min(requested_int, 32768))
        return min(requested_int, cap)

    def _process_response(self, response):
        self._response_content_blocks = []
        self._current_tool_calls = {}  # 改成字典，key 是 tool_call_id
        self._tool_calls_buffer = {}
        tool_calls_found = False
        tool_args_pending = True
        for chunk in response:
            if self._is_cancelled:
                return False, False  # 返回元组而不是单个布尔值

            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)

            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                tool_calls_found = True
                for tc in tool_calls:
                    tc_id = tc.id
                    if tc_id is None:
                        if self._tool_calls_buffer:
                            tc_id = list(self._tool_calls_buffer.keys())[-1]
                        else:
                            continue

                    if tc_id not in self._tool_calls_buffer:
                        self._tool_calls_buffer[tc_id] = {
                            "id": tc_id,
                            "type": getattr(tc, "type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                        self._response_content_blocks.append(
                            {
                                "type": "tool_call_marker",
                                "tool_call_id": tc_id,
                            }
                        )

                    buffer = self._tool_calls_buffer[tc_id]
                    tool_name = ""
                    if tc.function and tc.function.name:
                        buffer["function"]["name"] = tc.function.name
                        tool_name = buffer["function"]["name"]
                        
                        # 收到 tool name 时立即添加到 _current_tool_calls（如果是新工具）
                        if tc_id not in self._current_tool_calls:
                            self._current_tool_calls[tc_id] = {
                                "id": tc_id,
                                "type": getattr(tc, "type", "function"),
                                "function": {
                                    "name": tool_name,
                                    "arguments": "",
                                },
                            }
                        
                        if (
                            tool_name
                            and tool_name not in self._DEFERRED_PREVIEW_TOOLS
                            and tc_id not in self._previewed_tool_call_ids
                        ):
                            self._previewed_tool_call_ids.add(tc_id)
                            # preview 阶段：arguments 可能还没接收完，显示 "加载中..." 而不是空 {}
                            preview_args = {"_status": "loading", "_preview_hint": "参数接收中..."}
                            if self.tool_start_callback:
                                self.tool_start_callback(
                                    tc_id, tool_name, preview_args, "preview"
                                )
                            else:
                                self._emit_with_callback(
                                    "tool_call_started", self.tool_call_started,
                                    tc_id, tool_name, preview_args, "preview"
                                )
                    if tc.function and tc.function.arguments:
                        buffer["function"]["arguments"] += tc.function.arguments

                    if buffer["function"]["name"] and buffer["function"]["arguments"]:
                        try:
                            parsed_args = json.loads(buffer["function"]["arguments"])
                            tool_args_pending = False
                            # 更新 _current_tool_calls 中对应 id 的 arguments
                            if tc_id in self._current_tool_calls:
                                self._current_tool_calls[tc_id]["function"]["arguments"] = buffer["function"]["arguments"]
                            # 标记已完成解析（用于决定是否发送 tool_call_started）
                            self._current_tool_calls[tc_id]["_args_parsed"] = True
                            self._tool_calls_buffer.pop(tc_id, None)
                        except json.JSONDecodeError:
                            # JSON 解析失败，记录到等待队列，等待后续 chunk
                            if tc_id not in self._waiting_tool_params:
                                self._waiting_tool_params[tc_id] = {
                                    "buffer": buffer,
                                    "attempt_count": 0,
                                    "first_failure_time": time.time(),
                                }
                            # 标记已尝试解析（即使失败）
                            self._waiting_tool_params[tc_id]["attempt_count"] += 1

            # 提取 reasoning_content (DeepSeek V4 thinking mode)
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                # 性能优化：使用 list append 代替字符串拼接
                self._reasoning_chunks.append(reasoning_delta)
                self._emit_with_callback("reasoning_content_received", self.reasoning_content_received, reasoning_delta)

            if content:
                # 性能优化：使用 list append + join 代替字符串拼接
                self._response_chunks.append(content)
                self._response_content_blocks = append_text_block(
                    self._response_content_blocks, content
                )
                self._emit_with_callback("content_received", self.content_received, content)

        # 处理等待完整参数的 tool_calls（超长 arguments 场景）
        # 在所有 chunk 接收完成后，再次尝试解析仍处于等待状态的 tool_calls
        # 性能优化：使用 update 代替创建临时集合
        all_pending_ids = set(self._tool_calls_buffer.keys())
        all_pending_ids.update(self._waiting_tool_params.keys())
        
        for tc_id in list(all_pending_ids):
            buffer = self._tool_calls_buffer.get(tc_id)
            waiting_info = self._waiting_tool_params.get(tc_id)
            
            # 如果 buffer 存在，优先使用 buffer
            if not buffer and waiting_info:
                buffer = waiting_info["buffer"]
            
            if buffer and buffer["function"]["name"] and buffer["function"]["arguments"]:
                args_str = buffer["function"]["arguments"]
                
                # 跳过已存在的 tc_id（已通过 tool name 添加到字典）
                if tc_id in self._current_tool_calls:
                    # 更新 arguments
                    self._current_tool_calls[tc_id]["function"]["arguments"] = args_str
                    self._waiting_tool_params.pop(tc_id, None)
                    self._tool_calls_buffer.pop(tc_id, None)
                    continue
                
                try:
                    # 尝试 JSON 解析
                    parsed_args = json.loads(args_str)
                    tool_args_pending = False
                    self._current_tool_calls[tc_id] = {
                        "id": buffer["id"],
                        "type": buffer.get("type", "function"),
                        "function": {
                            "name": buffer["function"]["name"],
                            "arguments": args_str,
                        },
                        "_args_parsed": True,
                    }
                    # 从等待队列中移除
                    self._waiting_tool_params.pop(tc_id, None)
                    self._tool_calls_buffer.pop(tc_id, None)
                except json.JSONDecodeError as e:
                    # JSON 仍然解析失败，记录详细错误信息
                    if tc_id in self._tool_calls_buffer:
                        self._tool_calls_buffer.pop(tc_id, None)
                    
                    # 检查是否超过最大重试次数
                    attempt_count = waiting_info.get("attempt_count", 0) if waiting_info else 0
                    first_time = waiting_info.get("first_failure_time", 0) if waiting_info else 0
                    wait_duration = time.time() - first_time if first_time else 0
                    
                    # 超过 60 秒或超过 10 次尝试，放弃解析
                    if wait_duration > 60 or attempt_count >= self._max_param_retry_count:
                        logger.warning(
                            f"[ToolCall] ⚠️ JSON 解析超时/超限，保留原始 arguments: "
                            f"tool={buffer['function']['name']}, "
                            f"args_len={len(args_str)}, "
                            f"attempt_count={attempt_count}, "
                            f"wait_duration={wait_duration:.1f}s, "
                            f"error={str(e)}, "
                            f"preview='{args_str[:100]}...'"
                        )
                        # 保留原始 arguments 字符串，让后续处理决定如何处理
                        if tc_id not in self._current_tool_calls:
                            self._current_tool_calls[tc_id] = {
                                "id": buffer["id"],
                                "type": buffer.get("type", "function"),
                                "function": {
                                    "name": buffer["function"]["name"],
                                    "arguments": args_str,  # 保留原始字符串
                                },
                            }
                        self._waiting_tool_params.pop(tc_id, None)
                    else:
                        # 还在等待中，保持在等待队列
                        if tc_id not in self._waiting_tool_params:
                            self._waiting_tool_params[tc_id] = {
                                "buffer": buffer,
                                "attempt_count": attempt_count + 1,
                                "first_failure_time": first_time,
                            }

        return tool_calls_found, tool_args_pending

    def _execute_all_tools(self):
        if not self._current_tool_calls or not self.tool_executor:
            return []

        # 重置工具执行取消标志，开始新的执行周期
        self._tool_execution_cancelled = False

        results = []
        for tc in self._current_tool_calls.values():
            if self._is_cancelled or self._tool_execution_cancelled:
                cancelled_tc_id = tc["id"]
                tool_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except:
                    args = {}
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    cancelled_tc_id,
                    tool_name,
                    args,
                    type(
                        "ToolResult",
                        (),
                        {"success": False, "content": None, "error": "用户中止"},
                    )(),
                )
                if not self._direct_callbacks:
                    QApplication.processEvents()
                self._is_cancelled = True
                return None

            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            tool_call_id = tc["id"]
            raw_args = tc["function"]["arguments"]
            round_id = f"round_{id(tc)}"
            original_args_str = arguments  # 保存原始字符串用于错误诊断

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    # JSON 解析失败，尝试智能修复（处理模型生成的不规范 JSON）
                    fixed_args = _smart_parse_arguments(arguments, tool_name)
                    if fixed_args is not None:
                        arguments = fixed_args
                        logger.info(
                            f"[ToolCall] ✓ JSON 智能修复成功: tool={tool_name}, "
                            f"args={list(arguments.keys())}"
                        )
                    elif not arguments or not arguments.strip():
                        # 确实是空字符串
                        arguments = {}
                    else:
                        # 无法修复，检查是否是参数太长
                        args_len = len(arguments)
                        if args_len > 10000:
                            # 参数太长，返回明确的错误提示
                            logger.warning(
                                f"[ToolCall] ⚠️ 工具参数过长: tool={tool_name}, "
                                f"args_len={args_len}, 请减少参数长度"
                            )
                            # 检查是否需要发送 tool_call_started 信号（参数在流式传输时未发送）
                            preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}

                            error_result = {
                                "success": False,
                                "content": None,
                                "error": f"[参数过长] 工具参数长度 {args_len} 字符，超过限制。\n"
                                         f"工具: {tool_name}\n"
                                         f"建议: 请减少参数长度（建议不超过 5000 字符），"
                                         f"可以将内容拆分为多次工具调用，例如先 write 文件头，"
                                         f"再用 edit 追加内容，或使用其他方式分批处理。",
                            }
                            self._emit_with_callback(
                                "tool_result_received",
                                self.tool_result_received,
                                tool_call_id, tool_name, preview_args,
                                error_result
                            )
                            if not self._direct_callbacks:
                                QApplication.processEvents()
                            results.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "arguments": {"_raw_args": raw_args[:500]},
                                "content": error_result["error"],
                                "success": False,
                                "round_id": round_id,
                            })
                            continue
                        else:
                            # 其他 JSON 解析错误
                            logger.warning(
                                f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, "
                                f"error={str(e)}, "
                                f"raw_args='{arguments[:300]}...'"
                            )
                            tool_call_id = tc["id"]
                            raw_args = tc["function"]["arguments"]
                            round_id = f"round_{id(tc)}"
                            
                            # 检查是否需要发送 tool_call_started 信号
                            preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
                            if self.tool_start_callback:
                                self.tool_start_callback(tool_call_id, tool_name, preview_args, round_id)
                            else:
                                self._emit_with_callback(
                                    "tool_call_started",
                                    self.tool_call_started,
                                    tool_call_id, tool_name, preview_args, round_id
                                )
                            
                            error_result = {
                                "success": False,
                                "content": None,
                                "error": f"[参数错误] JSON 格式无效: {str(e)}\n"
                                         f"工具: {tool_name}\n"
                                         f"原始内容(前500字): {arguments[:500]}...",
                            }
                            self._emit_with_callback(
                                "tool_result_received",
                                self.tool_result_received,
                                tool_call_id, tool_name, preview_args,
                                error_result
                            )
                            if not self._direct_callbacks:
                                QApplication.processEvents()
                            results.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "arguments": {"_raw_args": raw_args[:500]},
                                "content": error_result["error"],
                                "success": False,
                                "round_id": round_id,
                            })
                            continue

            # 检查必需参数
            required_args = self.tool_executor.REQUIRED_ARGS.get(tool_name, [])
            missing_args = [p for p in required_args if p not in arguments]
            if missing_args:
                logger.warning(
                    f"[ToolCall] ⚠️ 缺少必需参数: tool={tool_name}, missing={missing_args}, "
                    f"raw_args='{original_args_str if isinstance(original_args_str, str) else str(original_args_str)}'"
                )
                tool_call_id = tc["id"]
                round_id = f"round_{id(tc)}"
                error_result = {
                    "success": False,
                    "content": None,
                    "error": f"[参数缺失] 缺少必需参数: {missing_args}\n"
                             f"工具: {tool_name}",
                }
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    tool_call_id, tool_name, arguments,
                    error_result
                )
                if not self._direct_callbacks:
                    QApplication.processEvents()
                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                    "content": error_result["error"],
                    "success": False,
                    "round_id": round_id,
                })
                continue

            tool_call_id = tc["id"]

            round_id = f"round_{id(tc)}"
            if self.tool_start_callback:
                self.tool_start_callback(tool_call_id, tool_name, arguments, round_id)
            else:
                self._emit_with_callback(
                    "tool_call_started",
                    self.tool_call_started,
                    tool_call_id, tool_name, arguments, round_id
                )
                if not self._direct_callbacks:
                    QApplication.processEvents()

            if tool_name == "question":
                question_text = arguments.get("question", "")
                options = arguments.get("options", [])
                # 确保 options 是 list 类型（可能是模型生成时出错导致的字符串）
                if isinstance(options, str):
                    try:
                        options = json.loads(options)
                    except (json.JSONDecodeError, TypeError):
                        options = []
                multiple = arguments.get("multiple", False)
                self._emit_with_callback("question_asked", self.question_asked, tool_call_id, question_text, options, multiple)
                self._question_pending = {
                    "tool_call_id": tool_call_id,
                    "question": question_text,
                    "options": options,
                    "multiple": multiple,
                }
                return None

            if self.permission_check_callback:
                # 优先检查该轮缓存，再检查会话级缓存，最后才检查权限
                if tool_name in self._round_permission_cache:
                    permission_result = "allow"
                    logger.info(f"[Permission] 使用轮缓存: tool={tool_name}")
                elif tool_name in self._session_permission_cache:
                    permission_result = "allow"
                    logger.info(f"[Permission] 使用会话缓存: tool={tool_name}")
                else:
                    permission_result = self.permission_check_callback(
                        tool_name, arguments
                    )
                if permission_result == "ask":
                    self._emit_with_callback("permission_approval_requested", self.permission_approval_requested, tool_call_id, tool_name, arguments)
                    self._permission_pending = {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    }
                    self._permission_approved = False
                    while (
                        self._permission_pending is not None
                        and not self._is_cancelled
                        and not self._tool_execution_cancelled
                    ):
                        if not self._direct_callbacks:
                            QApplication.processEvents()
                        time.sleep(0.1)

                    if self._is_cancelled or self._tool_execution_cancelled:
                        cancelled_tc_id = tool_call_id
                        self._emit_with_callback(
                            "tool_result_received",
                            self.tool_result_received,
                            cancelled_tc_id,
                            tool_name,
                            arguments,
                            type(
                                "ToolResult",
                                (),
                                {
                                    "success": False,
                                    "content": None,
                                    "error": "用户中止",
                                },
                            )(),
                        )
                        if not self._direct_callbacks:
                            QApplication.processEvents()
                        self._is_cancelled = True
                        return None

                    if not self._permission_approved:
                        self._emit_with_callback(
                            "tool_result_received",
                            self.tool_result_received,
                            tool_call_id,
                            tool_name,
                            arguments,
                            type(
                                "ToolResult",
                                (),
                                {
                                    "success": False,
                                    "error": "Permission denied by user",
                                },
                            )(),
                        )
                        if not self._direct_callbacks:
                            QApplication.processEvents()
                        results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": "Error: Permission denied by user",
                                "round_id": round_id,
                            }
                        )
                        continue

            try:
                # 设置当前调用的 call_id（用于文件操作记录）
                if hasattr(self.tool_executor, "set_call_id"):
                    self.tool_executor.set_call_id(tool_call_id)

                # 传递取消标志引用，以便异步工具可以检测取消
                cancelled_ref = [self._is_cancelled or self._tool_execution_cancelled]
                result = self.tool_executor.execute(tool_name, arguments, cancelled_ref)
                # 更新取消标志引用
                cancelled_ref[0] = self._is_cancelled or self._tool_execution_cancelled
            except Exception as e:
                logger.error(f"[Tool] Tool '{tool_name}' execution failed: {e}")
                result = None
                result_content = f"Tool execution error: {str(e)}"
                success = False
            else:
                result_content = str(result) if result else ""
                success = bool(getattr(result, "success", True)) if result else False

            if self._is_cancelled or self._tool_execution_cancelled:
                # 创建取消结果对象（直接使用 dict 简化，避免循环导入）
                cancelled_result = {
                    "success": False,
                    "content": None,
                    "error": "用户中止",
                }
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    tool_call_id, tool_name, arguments, cancelled_result
                )
                if not self._direct_callbacks:
                    QApplication.processEvents()
                self._is_cancelled = True
                return None

            self._emit_with_callback("tool_result_received", self.tool_result_received, tool_call_id, tool_name, arguments, result)
            if not self._direct_callbacks:
                QApplication.processEvents()
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments or {},
                    "content": result_content,
                    "success": success,
                    "round_id": round_id,
                }
            )

        return results

    def _check_and_notify_stage_change(self):
        if not self.stage_changed_callback:
            return

        import re

        pattern = re.compile(r"\[STAGE:\s*(\w+)\]", re.IGNORECASE)
        matches = pattern.findall(self.full_response)

        if matches:
            new_stage = matches[-1].lower()
            self.stage_changed_callback(new_stage)

    def _handle_error(self, error):
        from openai import (
            BadRequestError,
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            APIError,
        )

        error_msg = str(error)

        if (
            "peer closed connection" in error_msg.lower()
            or "incomplete chunked read" in error_msg.lower()
        ):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接中断] 服务器在响应中途关闭了连接，可能是服务器过载或网络不稳定。请稍后重试。"
            )
            return
        if "ProtocolError" in error_msg or "RemoteProtocolError" in error_msg:
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接错误] 网络协议错误，可能是服务器关闭了连接。请稍后重试。"
            )
            return

        if isinstance(error, BadRequestError):
            if "json" in error_msg.lower() or "format" in error_msg.lower():
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[JSON格式错误] 请确保输入有效的JSON格式: {error_msg}"
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[请求错误] {error_msg}")
        elif isinstance(error, RateLimitError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[速率限制] 请求过于频繁，请稍后再试。详情: {error_msg}"
            )
        elif isinstance(error, APIConnectionError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接失败] 无法连接到 API 服务器，请检查网络或 API_URL 设置。详情: {error_msg}"
            )
        elif isinstance(error, APITimeoutError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[超时] 请求超时（300秒），请检查网络或模型负载。详情: {error_msg}"
            )
        elif isinstance(error, APIError):
            if "context length" in error_msg and "overflow" in error_msg:
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[上下文超限] 输入内容过长，请缩短对话或清除历史记录。详情: {error_msg}"
                )
            elif "insufficient_quota" in error_msg:
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[配额不足] API配额已用完，请检查账户余额或更换API Key。"
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[API错误] {error_msg}")
        elif "unrecognized_parameter" in error_msg or "extra_parameters" in error_msg:
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[兼容性提示] 当前模型可能不支持某些高级设置（如思考模式或温度）。错误: {error_msg}"
            )
        elif "max_tokens" in error_msg.lower() or "context length" in error_msg.lower():
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[错误] 模型上下文或最大Token超出限制，请减少输入长度或调低 max_tokens"
            )
        elif "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            self._emit_with_callback("error_occurred", self.error_occurred, f"[认证错误] API Key无效或已过期，请检查配置。")
        else:
            self._emit_with_callback("error_occurred", self.error_occurred, f"[未知错误] {error_msg}")