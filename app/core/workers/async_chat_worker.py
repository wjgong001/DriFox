# -*- coding: utf-8 -*-
"""
Async Chat Worker - OpenAI 对话执行器（无 PyQt 依赖版本）
使用 threading.Thread 替代 QThread，回调替代 pyqtSignal
"""

import json
import re
import time
import httpcore
import httpx
import threading
from collections import deque
from datetime import datetime
from loguru import logger
from typing import Any, Dict, List, Callable, Optional, Tuple
from openai import (
    OpenAI, BadRequestError, RateLimitError, APIError, APIConnectionError,
)

from app.core.memory_manager import MEMORY_CATEGORIES
from app.core.provider_profile import get_provider_profile
from app.core.message_content import consolidate_messages, append_text_block, messages_to_api, to_api_message


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


class AsyncOpenAIChatWorker:
    """
    异步聊天 Worker（无 PyQt 依赖版本）
    使用 threading.Thread 替代 QThread，回调函数替代 pyqtSignal
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
    ):
        # 消息和配置
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
        
        # 响应状态
        self.full_response = ""
        self._response_chunks: deque = deque()
        self._is_cancelled = False
        self._question_pending = None
        self._pending_answer = None
        self._answer_event = threading.Event()
        self._permission_pending = None
        self._permission_approved = False
        self._round_permission_cache = {}
        self._session_permission_cache = {}
        self._previewed_tool_call_ids = set()
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        self._reasoning_content = ""
        
        # HTTP 客户端和参数缓存
        self._http_client: Optional[Any] = None
        self._cached_api_config: Optional[Dict[str, Any]] = None
        self._reasoning_chunks: deque = deque()
        self._response_content_blocks = []
        self._tool_execution_cancelled = False
        
        # 等待完整参数的 tool_calls
        self._waiting_tool_params: Dict[str, dict] = {}
        self._max_param_retry_count = 10
        
        # 压缩状态
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
        
        # API 消息缓存
        self._api_messages_cache: Optional[List[Dict[str, Any]]] = None
        self._api_messages_built = False
        
        # 回调函数（替代 Qt 信号）
        self._callbacks: Dict[str, Callable] = {}
        
        # 执行线程
        self._thread: Optional[threading.Thread] = None
    
    def set_direct_callbacks(self, callbacks: Dict[str, Callable]) -> None:
        """设置直接回调（API 模式专用）"""
        self._callbacks = callbacks

    def _emit(self, signal_name: str, *args) -> None:
        """发射回调（替代 Qt 信号发射）"""
        callback = self._callbacks.get(signal_name)
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"[Worker] Callback error for {signal_name}: {e}")

    def start(self) -> None:
        """启动工作线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._is_cancelled = True
        self._tool_execution_cancelled = True
        self._answer_event.set()
        if self._question_pending:
            self._question_pending = None
        if self._permission_pending:
            self._permission_pending = None

    def get_interrupted_messages(self) -> List[Dict]:
        snapshot = list(self._current_session_messages or self.session_messages or [])
        partial_sequence = self._build_response_message_sequence()
        if partial_sequence:
            snapshot.extend(partial_sequence)
        return consolidate_messages(snapshot)

    def _clear_pending_response_state(self):
        """清理单轮对话结束后的中间状态"""
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        self._waiting_tool_params = {}
        self._previewed_tool_call_ids = set()
        self._response_content_blocks = []
        self._reasoning_content = ""
        self._reasoning_chunks = deque()

    def _get_reasoning_content(self) -> str:
        """获取当前的 reasoning_content"""
        if self._reasoning_chunks:
            return ''.join(self._reasoning_chunks)
        return self._reasoning_content

    def cleanup(self):
        """彻底清理 worker 的所有缓存数据"""
        msg_count = len(self.messages) + len(self.session_messages) + len(self._current_session_messages or [])
        full_resp_len = len(self.full_response or "")
        reasoning_len = len(self._reasoning_content or "")
        blocks_count = len(self._response_content_blocks or [])
        
        if full_resp_len > 100000 or reasoning_len > 100000:
            logger.info(f"[Worker] 清理大量缓存: full_response={full_resp_len/1024:.1f}KB, "
                       f"reasoning={reasoning_len/1024:.1f}KB, messages={msg_count}, blocks={blocks_count}")
        
        self.messages = []
        self.session_messages = []
        self._current_session_messages = []
        self.full_response = ""
        self._reasoning_content = ""
        self._reasoning_chunks = deque()
        self._response_content_blocks = []
        self._response_chunks = deque()
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        self._waiting_tool_params = {}
        self._previewed_tool_call_ids = set()
        self._api_messages_cache = None
        self._api_messages_built = False
        self.tools = []
        self._callbacks = {}
        self._pending_answer = None
        self._question_pending = None
        self._http_client = None
        self._cached_api_config = None

    def _get_http_client(self) -> Any:
        """获取或创建复用的 HTTP 客户端"""
        if self._http_client is None:
            self._http_client = OpenAI(
                api_key=self.llm_config.get("API_KEY", "").strip(),
                base_url=self.llm_config.get("API_URL"),
                timeout=httpx.Timeout(600.0, connect=60.0),
            )
        return self._http_client

    def _build_api_request_kwargs(self) -> Dict[str, Any]:
        """预构建 API 请求参数"""
        config_key = str(self.llm_config.get("API_KEY", "")) + str(self.llm_config.get("API_URL", ""))
        
        if self._cached_api_config is not None and self._cached_api_config.get("_config_key") == config_key:
            return {
                "model": self._cached_api_config["model"],
                "stream": self.stream,
                "extra_body": dict(self._cached_api_config.get("extra_body") or {}),
                "_auth_headers": self._cached_api_config.get("_auth_headers"),
                "_is_o1_model": self._cached_api_config.get("_is_o1_model"),
            }
        
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
                continue
            extra_body[en_key] = value
        
        max_tokens = self.llm_config.get("最大Token")
        if max_tokens is not None:
            extra_body["max_tokens"] = self._cap_max_output_tokens(model, max_tokens)
        
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
        """设置会话级权限缓存"""
        if allowed:
            self._session_permission_cache[tool_name] = True
        elif tool_name in self._session_permission_cache:
            del self._session_permission_cache[tool_name]

    def approve_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
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

    def _run(self):
        """主执行循环"""
        try:
            current_messages = self.messages.copy()
            current_session_messages = list(self.session_messages)
            self._current_session_messages = list(current_session_messages)
            self._emit("compaction_status_changed", dict(self._last_compaction_state))
            self.full_response = ""
            self._reasoning_content = ""
            self._clear_pending_response_state()
            self._api_messages_cache = None
            self._api_messages_built = False
            self._round_permission_cache = {}

            while not self._is_cancelled:
                if self._is_cancelled:
                    return
                
                self._clear_pending_response_state()
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
                    self._append_to_api_cache(response_sequence)
                    self.full_response = ''.join(self._response_chunks)
                    self._emit("finished_with_messages", current_session_messages)
                    self._clear_pending_response_state()
                    self._emit("finished_with_content", self.full_response)
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
                    self._append_to_api_cache(response_sequence + [question_result])
                    self._emit("finished_with_messages", current_session_messages)
                    self._question_pending = None
                    self._pending_answer = None
                    self._answer_event.clear()
                    continue
                    
                response_sequence = self._build_response_message_sequence(tool_results)
                current_messages.extend(response_sequence)
                current_session_messages.extend(response_sequence)
                self._current_session_messages = list(current_session_messages)
                self._append_to_api_cache(response_sequence)
                self._emit("finished_with_messages", current_session_messages)

                self._check_and_notify_stage_change()
                time.sleep(0.01)

        except Exception as e:
            logger.exception("请求失败!")
            self._handle_error(e)
        finally:
            self._round_permission_cache = {}

    def _build_api_messages_cache(self) -> List[Dict[str, Any]]:
        """构建 API 消息缓存"""
        if self._api_messages_cache is not None:
            return self._api_messages_cache
        
        self._api_messages_cache = messages_to_api(self.messages)
        self._api_messages_built = True
        return self._api_messages_cache
    
    def _append_to_api_cache(self, new_messages: List[Dict[str, Any]]) -> None:
        """将新消息追加到 API 缓存"""
        if self._api_messages_cache is None:
            self._api_messages_cache = messages_to_api(new_messages)
            return
        
        for msg in new_messages:
            api_msg = to_api_message(msg)
            if api_msg:
                if api_msg.get("role") == "user" and not api_msg.get("content"):
                    continue
                self._api_messages_cache.append(api_msg)

    def _build_response_message_sequence(self, tool_results=None) -> List[Dict]:
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reasoning_content = self._get_reasoning_content()
        
        tool_call_map = {}
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
        self._emit("compaction_status_changed", dict(normalized))

    def _fix_tool_result_order(self, messages: List[Dict]) -> tuple[List[Dict], bool]:
        """修复消息列表中 tool result 顺序问题"""
        fixed_messages: List[Dict] = []
        modified = False
        
        valid_tool_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id:
                    valid_tool_call_ids.add(tc_id)
        
        logger.warning(f"[ToolCall修复] 有效 tool_call_ids: {len(valid_tool_call_ids)} 个")
        
        if not valid_tool_call_ids:
            has_tool_calls = any(
                msg.get("role") == "assistant" and msg.get("tool_calls")
                for msg in messages
            )
            if has_tool_calls:
                logger.warning("[ToolCall修复] 检测到用户中断场景：assistant 有 tool_calls 但无任何 tool 结果")
                for msg in messages:
                    if msg.get("role") == "assistant":
                        if msg.get("tool_calls"):
                            msg["tool_calls"] = []
                            modified = True
                            logger.info("[ToolCall修复] 已移除中断时的 tool_calls")
                return messages, modified
            return messages, False
        
        for msg in messages:
            if msg.get("role") != "assistant":
                fixed_messages.append(msg)
                continue
            
            fixed_msg = dict(msg)
            tool_calls = fixed_msg.get("tool_calls") or []
            
            if not tool_calls:
                fixed_messages.append(fixed_msg)
                continue
            
            seen_ids: set = set()
            new_tool_calls: List[Dict] = []
            removed_count = 0
            
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                
                if tc_id in seen_ids:
                    logger.warning(f"[ToolCall修复] 发现重复 tool_call_id: {tc_id[:20]}...，已移除")
                    modified = True
                    removed_count += 1
                    continue
                
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
                fixed_msg.pop("tool_calls", None)
                logger.info("[ToolCall修复] 所有 tool_call 均无对应结果，已移除 tool_calls 字段")
            
            fixed_messages.append(fixed_msg)
        
        return fixed_messages, modified

    def _try_recover_tool_arguments(self, messages: List[Dict]) -> Optional[List[Dict]]:
        """尝试从历史消息中恢复 tool_calls 的参数"""
        try:
            tool_calls_by_content_hash: Dict[str, Dict] = {}
            
            for msg in messages:
                if msg.get("role") == "assistant":
                    tool_calls = msg.get("tool_calls") or []
                    for tc in tool_calls:
                        function = tc.get("function", {}) or {}
                        arguments = function.get("arguments", "{}")
                        args_hash = str(arguments)[:100]
                        if args_hash:
                            tool_calls_by_content_hash[args_hash] = tc
            
            if not tool_calls_by_content_hash:
                logger.warning("[ToolCall恢复] 未找到任何 tool_calls，无法恢复参数")
                return None
            
            logger.info(f"[ToolCall恢复] 找到 {len(tool_calls_by_content_hash)} 个 tool_calls 用于参数匹配")
            return None
            
        except Exception as e:
            logger.warning(f"[ToolCall恢复] 尝试恢复工具参数时出错: {e}")
            return None

    def _make_api_call(self, messages: List[Dict], use_cache: bool = True) -> bool:
        """发起 API 调用"""
        if use_cache and self._api_messages_cache is not None:
            sanitized = self._api_messages_cache
        else:
            sanitized = messages_to_api(messages)
            if use_cache:
                self._api_messages_cache = sanitized
                self._api_messages_built = True
        
        cached_config = self._build_api_request_kwargs()
        
        req_kwargs: Dict[str, Any] = {
            "model": cached_config["model"],
            "messages": sanitized,
            "stream": cached_config["stream"],
        }
        
        if cached_config.get("extra_body"):
            req_kwargs["extra_body"] = cached_config["extra_body"]
        
        if cached_config.get("_auth_headers"):
            req_kwargs["extra_headers"] = cached_config["auth_headers"]
        
        if self.tools:
            req_kwargs["tools"] = self.tools
        
        if cached_config.get("_is_o1_model"):
            req_kwargs.pop("stream", None)
            self.stream = False

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
                is_tool_call_order_error = "2013" in error_str or "tool call result does not follow tool call" in error_str.lower()
                
                if is_tool_call_order_error and attempt < max_retries - 1:
                    logger.warning(f"[API] 检测到 tool call result 顺序错误 (2013)，尝试自动修复...")
                    
                    fixed_messages, was_fixed = self._fix_tool_result_order(req_kwargs["messages"])
                    
                    if was_fixed:
                        req_kwargs["messages"] = messages_to_api(fixed_messages)
                        logger.warning(f"[API] 已修复消息顺序，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.error(f"[API] 无法自动修复 tool call result 顺序问题")
                        
                is_missing_args_error = "Missing required arguments" in error_str or "missing a required argument" in error_str.lower()
                
                if is_missing_args_error and attempt < max_retries - 1:
                    logger.warning(f"[API] 检测到工具参数丢失错误，尝试从历史消息中恢复...")
                    
                    fixed_messages = self._try_recover_tool_arguments(req_kwargs["messages"])
                    
                    if fixed_messages is not None:
                        req_kwargs["messages"] = messages_to_api(fixed_messages)
                        logger.warning(f"[API] 已恢复工具参数，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.warning(f"[API] 无法恢复工具参数，保持现有消息")
                        
                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                raise
            except Exception as e:
                last_error = e
                error_str = str(e)
                error_type = type(e).__name__

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
        self._current_tool_calls = {}
        self._tool_calls_buffer = {}
        tool_calls_found = False
        tool_args_pending = True
        for chunk in response:
            if self._is_cancelled:
                return False, False

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
                            preview_args = {"_status": "loading", "_preview_hint": "参数接收中..."}
                            if self.tool_start_callback:
                                self.tool_start_callback(
                                    tc_id, tool_name, preview_args, "preview"
                                )
                            else:
                                self._emit(
                                    "tool_call_started", tc_id, tool_name, preview_args, "preview"
                                )
                    if tc.function and tc.function.arguments:
                        buffer["function"]["arguments"] += tc.function.arguments

                    if buffer["function"]["name"] and buffer["function"]["arguments"]:
                        try:
                            parsed_args = json.loads(buffer["function"]["arguments"])
                            tool_args_pending = False
                            if tc_id in self._current_tool_calls:
                                self._current_tool_calls[tc_id]["function"]["arguments"] = buffer["function"]["arguments"]
                            self._current_tool_calls[tc_id]["_args_parsed"] = True
                            self._tool_calls_buffer.pop(tc_id, None)
                        except json.JSONDecodeError:
                            if tc_id not in self._waiting_tool_params:
                                self._waiting_tool_params[tc_id] = {
                                    "buffer": buffer,
                                    "attempt_count": 0,
                                    "first_failure_time": time.time(),
                                }
                            self._waiting_tool_params[tc_id]["attempt_count"] += 1

            # 提取 reasoning_content (DeepSeek V4 thinking mode)
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                self._reasoning_chunks.append(reasoning_delta)
                self._emit("reasoning_content_received", reasoning_delta)

            if content:
                self._response_chunks.append(content)
                self._response_content_blocks = append_text_block(
                    self._response_content_blocks, content
                )
                self._emit("content_received", content)

        # 处理等待完整参数的 tool_calls
        all_pending_ids = set(self._tool_calls_buffer.keys())
        all_pending_ids.update(self._waiting_tool_params.keys())
        
        for tc_id in list(all_pending_ids):
            buffer = self._tool_calls_buffer.get(tc_id)
            waiting_info = self._waiting_tool_params.get(tc_id)
            
            if not buffer and waiting_info:
                buffer = waiting_info["buffer"]
            
            if buffer and buffer["function"]["name"] and buffer["function"]["arguments"]:
                args_str = buffer["function"]["arguments"]
                
                if tc_id in self._current_tool_calls:
                    self._current_tool_calls[tc_id]["function"]["arguments"] = args_str
                    self._waiting_tool_params.pop(tc_id, None)
                    self._tool_calls_buffer.pop(tc_id, None)
                    continue
                
                try:
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
                    self._waiting_tool_params.pop(tc_id, None)
                    self._tool_calls_buffer.pop(tc_id, None)
                except json.JSONDecodeError as e:
                    if tc_id in self._tool_calls_buffer:
                        self._tool_calls_buffer.pop(tc_id, None)
                    
                    attempt_count = waiting_info.get("attempt_count", 0) if waiting_info else 0
                    first_time = waiting_info.get("first_failure_time", 0) if waiting_info else 0
                    wait_duration = time.time() - first_time if first_time else 0
                    
                    if wait_duration > 60 or attempt_count >= self._max_param_retry_count:
                        logger.warning(
                            f"[ToolCall] ⚠️ JSON 解析超时/超限，保留原始 arguments: "
                            f"tool={buffer['function']['name']}, "
                            f"args_len={len(args_str)}, "
                            f"attempt_count={attempt_count}, "
                            f"wait_duration={wait_duration:.1f}s"
                        )
                        if tc_id not in self._current_tool_calls:
                            self._current_tool_calls[tc_id] = {
                                "id": buffer["id"],
                                "type": buffer.get("type", "function"),
                                "function": {
                                    "name": buffer["function"]["name"],
                                    "arguments": args_str,
                                },
                            }
                        self._waiting_tool_params.pop(tc_id, None)
                    else:
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
                self._emit(
                    "tool_result_received",
                    cancelled_tc_id,
                    tool_name,
                    args,
                    type(
                        "ToolResult",
                        (),
                        {"success": False, "content": None, "error": "用户中止"},
                    )(),
                )
                self._is_cancelled = True
                return None

            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            tool_call_id = tc["id"]
            raw_args = tc["function"]["arguments"]
            round_id = f"round_{id(tc)}"
            original_args_str = arguments

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    fixed_args = _smart_parse_arguments(arguments, tool_name)
                    if fixed_args is not None:
                        arguments = fixed_args
                        logger.info(
                            f"[ToolCall] ✓ JSON 智能修复成功: tool={tool_name}, "
                            f"args={list(arguments.keys())}"
                        )
                    elif not arguments or not arguments.strip():
                        arguments = {}
                    else:
                        args_len = len(arguments)
                        if args_len > 10000:
                            logger.warning(
                                f"[ToolCall] ⚠️ 工具参数过长: tool={tool_name}, "
                                f"args_len={args_len}, 请减少参数长度"
                            )
                            preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
                            error_result = {
                                "success": False,
                                "content": None,
                                "error": f"[参数过长] 工具参数长度 {args_len} 字符，超过限制。",
                            }
                            self._emit(
                                "tool_result_received",
                                tool_call_id, tool_name, preview_args,
                                error_result
                            )
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
                            logger.warning(
                                f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, "
                                f"error={str(e)}, raw_args='{arguments[:300]}...'"
                            )
                            preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
                            if self.tool_start_callback:
                                self.tool_start_callback(tool_call_id, tool_name, preview_args, round_id)
                            else:
                                self._emit(
                                    "tool_call_started",
                                    tool_call_id, tool_name, preview_args, round_id
                                )
                            error_result = {
                                "success": False,
                                "content": None,
                                "error": f"[参数错误] JSON 格式无效: {str(e)}",
                            }
                            self._emit(
                                "tool_result_received",
                                tool_call_id, tool_name, preview_args,
                                error_result
                            )
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

            required_args = self.tool_executor.REQUIRED_ARGS.get(tool_name, [])
            missing_args = [p for p in required_args if p not in arguments]
            if missing_args:
                logger.warning(
                    f"[ToolCall] ⚠️ 缺少必需参数: tool={tool_name}, missing={missing_args}"
                )
                error_result = {
                    "success": False,
                    "content": None,
                    "error": f"[参数缺失] 缺少必需参数: {missing_args}\n工具: {tool_name}",
                }
                self._emit(
                    "tool_result_received",
                    tool_call_id, tool_name, arguments,
                    error_result
                )
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

            round_id = f"round_{id(tc)}"
            if self.tool_start_callback:
                self.tool_start_callback(tool_call_id, tool_name, arguments, round_id)
            else:
                self._emit(
                    "tool_call_started",
                    tool_call_id, tool_name, arguments, round_id
                )

            if tool_name == "question":
                question_text = arguments.get("question", "")
                options = arguments.get("options", [])
                if isinstance(options, str):
                    try:
                        options = json.loads(options)
                    except (json.JSONDecodeError, TypeError):
                        options = []
                multiple = arguments.get("multiple", False)
                self._emit("question_asked", tool_call_id, question_text, options, multiple)
                self._question_pending = {
                    "tool_call_id": tool_call_id,
                    "question": question_text,
                    "options": options,
                    "multiple": multiple,
                }
                return None

            if self.permission_check_callback:
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
                    self._emit("permission_approval_requested", tool_call_id, tool_name, arguments)
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
                        time.sleep(0.1)

                    if self._is_cancelled or self._tool_execution_cancelled:
                        cancelled_tc_id = tool_call_id
                        self._emit(
                            "tool_result_received",
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
                        self._is_cancelled = True
                        return None

                    if not self._permission_approved:
                        self._emit(
                            "tool_result_received",
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
                if hasattr(self.tool_executor, "set_call_id"):
                    self.tool_executor.set_call_id(tool_call_id)
                cancelled_ref = [self._is_cancelled or self._tool_execution_cancelled]
                result = self.tool_executor.execute(tool_name, arguments, cancelled_ref)
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
                cancelled_result = {
                    "success": False,
                    "content": None,
                    "error": "用户中止",
                }
                self._emit(
                    "tool_result_received",
                    tool_call_id, tool_name, arguments, cancelled_result
                )
                self._is_cancelled = True
                return None

            self._emit("tool_result_received", tool_call_id, tool_name, arguments, result)
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
            self._emit(
                "error_occurred",
                f"[连接中断] 服务器在响应中途关闭了连接，可能是服务器过载或网络不稳定。请稍后重试。"
            )
            return
        if "ProtocolError" in error_msg or "RemoteProtocolError" in error_msg:
            self._emit(
                "error_occurred",
                f"[连接错误] 网络协议错误，可能是服务器关闭了连接。请稍后重试。"
            )
            return

        if isinstance(error, BadRequestError):
            if "json" in error_msg.lower() or "format" in error_msg.lower():
                self._emit(
                    "error_occurred",
                    f"[JSON格式错误] 请确保输入有效的JSON格式: {error_msg}"
                )
            else:
                self._emit("error_occurred", f"[请求错误] {error_msg}")
        elif isinstance(error, RateLimitError):
            self._emit(
                "error_occurred",
                f"[速率限制] 请求过于频繁，请稍后再试。详情: {error_msg}"
            )
        elif isinstance(error, APIConnectionError):
            self._emit(
                "error_occurred",
                f"[连接失败] 无法连接到 API 服务器，请检查网络或 API_URL 设置。详情: {error_msg}"
            )
        elif isinstance(error, APITimeoutError):
            self._emit(
                "error_occurred",
                f"[超时] 请求超时（300秒），请检查网络或模型负载。详情: {error_msg}"
            )
        elif isinstance(error, APIError):
            if "context length" in error_msg and "overflow" in error_msg:
                self._emit(
                    "error_occurred",
                    f"[上下文超限] 输入内容过长，请缩短对话或清除历史记录。详情: {error_msg}"
                )
            elif "insufficient_quota" in error_msg:
                self._emit(
                    "error_occurred",
                    f"[配额不足] API配额已用完，请检查账户余额或更换API Key。"
                )
            else:
                self._emit("error_occurred", f"[API错误] {error_msg}")
        elif "unrecognized_parameter" in error_msg or "extra_parameters" in error_msg:
            self._emit(
                "error_occurred",
                f"[兼容性提示] 当前模型可能不支持某些高级设置（如思考模式或温度）。错误: {error_msg}"
            )
        elif "max_tokens" in error_msg.lower() or "context length" in error_msg.lower():
            self._emit(
                "error_occurred",
                f"[错误] 模型上下文或最大Token超出限制，请减少输入长度或调低 max_tokens"
            )
        elif "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            self._emit("error_occurred", f"[认证错误] API Key无效或已过期，请检查配置。")
        else:
            self._emit("error_occurred", f"[未知错误] {error_msg}")
