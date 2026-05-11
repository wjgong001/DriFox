# -*- coding: utf-8 -*-
"""
聊天引擎模块 - 处理 LLM 对话的核心逻辑
"""
import anyio
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

from loguru import logger

from app.core.agent import PermissionResolver
from app.core.chat_session import (
    ChatSession,
    SessionManager,
)
from app.core.history_compactor import HistoryCompactor
from app.core.message_content import (
    consolidate_messages,
)
from app.core.permission_cache import PermissionCache
from app.core.provider_profile import (
    get_provider_profile,
)
from app.core.token_estimator import count_messages_tokens, estimate_tokens
from app.core.workers import OpenAIChatWorker
from app.core.workers.qt_signal_adapter import QtSignalAdapter
from app.tools import get_builtin_tools_schema
from app.utils.config import Settings


class ChatEngine:
    """聊天引擎，负责组装上下文并驱动 worker。"""

    def __init__(
        self,
        session_manager: SessionManager,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Optional[Any] = None,
        agent_manager: Any = None,
        get_chat_cards: Callable[[], List[Any]] = None,
        get_memory_context: Optional[Callable[[], str]] = None,
        worker_callbacks: Optional[Dict[str, Callable]] = None,
        api_mode: bool = False,
    ):
        self._session_manager = session_manager
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._get_chat_cards = get_chat_cards
        self._get_memory_context = get_memory_context

        self._current_worker: Optional[OpenAIChatWorker] = None
        self._is_streaming = False
        self._callbacks: Dict[str, Callable] = {}
        # 权限缓存：整个会话生命周期保存，不受 worker 新建影响
        self._permission_cache = PermissionCache()
        self._current_agent: Optional[str] = "plan"
        
        # API 模式专用：直接回调（绕过 Qt 信号-槽，避免跨线程事件循环问题）
        self._worker_callbacks = worker_callbacks or {}
        self._api_mode = api_mode
        
        self._compactor = HistoryCompactor(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
        )
        
        # QtSignalAdapter：事件总线到 PyQt 信号的适配器（UI 模式使用）
        self._qt_signal_adapter: Optional[QtSignalAdapter] = None
    
    @property
    def event_bus(self) -> Any:
        """获取当前 worker 的事件总线（如有）"""
        if self._current_worker is not None:
            return self._current_worker.event_bus
        return None

    @property
    def compactor(self) -> 'HistoryCompactor':
        """暴露压缩器供外部使用（如工具迭代中压缩）"""
        return self._compactor

    def _get_agent_manager(self):
        return self._agent_manager

    def set_session_manager(self, session_manager):
        """Update the session manager reference (used when session is archived)."""
        self._session_manager = session_manager

    def _check_tool_permission(self, tool_name: str, arguments: dict) -> str:
        agent_manager = self._get_agent_manager()
        if not agent_manager or not self._current_agent:
            return "allow"

        try:
            agent = agent_manager.get_agent(self._current_agent)
            if not agent:
                logger.warning(f"[_check_tool_permission] Agent not found: {self._current_agent}")
                return "allow"

            perm_resolver = PermissionResolver(agent.permission, {}, agent.tools)
            result = perm_resolver.resolve(tool_name)
            logger.info(f"[_check_tool_permission] agent={self._current_agent}, tool={tool_name}, result={result}")

            if tool_name == "bash":
                command = arguments.get("command", "")
                return perm_resolver.resolve(tool_name, command)
            elif tool_name in ("read", "edit", "write", "patch", "multiedit"):
                file_path = arguments.get("filePath", "")
                return perm_resolver.resolve(tool_name, file_path)
            elif tool_name == "webfetch":
                url = arguments.get("url", "")
                return perm_resolver.resolve(tool_name, url)
            elif tool_name == "websearch":
                query = arguments.get("query", "")
                return perm_resolver.resolve(tool_name, query)
            elif tool_name == "task_batch":
                # task_batch 支持多个子智能体，检查第一个的权限
                tasks = arguments.get("tasks", [])
                if tasks and len(tasks) > 0:
                    first_agent = tasks[0].get("agent", "")
                    return perm_resolver.resolve_task(first_agent)
                return perm_resolver.resolve_task("")
            elif tool_name == "skill":
                skill_name = arguments.get("name", "")
                return perm_resolver.resolve(tool_name, skill_name)
            else:
                return perm_resolver.resolve(tool_name)

        except Exception as e:
            logger.warning(f"[ChatEngine] Permission check error: {e}")
            return "allow"

    def _on_permission_approval_requested(
        self, tool_call_id: str, tool_name: str, arguments: dict
    ):
        self._emit("permission_approval_requested", tool_call_id, tool_name, arguments)

    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具权限请求"""
        if self._current_worker:
            # 转发给 Worker 处理
            self._current_worker.approve_permission(tool_call_id, auto_allow, session_allow)

    def deny_tool_permission(self, tool_call_id: str):
        if self._current_worker:
            self._current_worker.deny_permission(tool_call_id)

    def clear_session_permission_cache(self, tool_name: str = None):
        """清除会话级权限缓存"""
        if tool_name:
            self._permission_cache.deny(tool_name)
        else:
            self._permission_cache.clear_session()

    def set_callback(self, event: str, callback: Callable):
        self._callbacks[event] = callback

    def _emit(self, event: str, *args, **kwargs):
        # API 模式优先使用 _worker_callbacks
        callback = self._callbacks.get(event)
        if not callback and self._api_mode:
            callback = self._worker_callbacks.get(event)
        
        if callback:
            callback(*args, **kwargs)

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def current_agent(self) -> Optional[str]:
        return self._current_agent

    def switch_agent(self, agent_name: Optional[str]):
        agent_manager = self._get_agent_manager()
        if agent_name is None or agent_name.lower() in ("default", "通用"):
            self._current_agent = "plan"
            logger.info("[ChatEngine] Switched to default agent: plan")
            self._emit("agent_switched", "plan")
            return

        agent = agent_manager.get_agent(agent_name)
        if not agent:
            logger.warning(f"[ChatEngine] Agent not found: {agent_name}")
            return

        self._current_agent = agent_name
        logger.info(f"[ChatEngine] Switched to agent: {agent_name}")
        self._emit("agent_switched", agent_name)

    def send_message(
        self,
        user_text: str,
        context_params: Optional[Dict] = None,
    ) -> bool:
        if self._is_streaming:
            logger.warning("[ChatEngine] Already streaming, ignoring new message")
            return False

        session = self._session_manager.get_current_session()
        if not session:
            logger.error("[ChatEngine] No current session")
            return False

        llm_config = self._get_model_config()
        if not llm_config:
            logger.error("[ChatEngine] No LLM config available")
            self._emit("error", "配置无效，请检查模型设置")
            return False

        session.add_user_message(content=user_text, params=context_params or {})
        self._is_streaming = True

        self._emit("user_message_added", user_text)

        messages = self._build_messages(session, llm_config)
        if self._current_agent:
            available_tools = self._get_agent_manager().get_agent_tools_schema(
                self._current_agent
            )
        else:
            available_tools = get_builtin_tools_schema(self._get_agent_manager())

        self._start_worker(messages, llm_config, available_tools)
        return True

    def _build_messages(
        self,
        session: ChatSession,
        llm_config: Dict,
        allow_llm_summary: bool = False,
    ) -> List[Dict]:
        messages: List[Dict[str, Any]] = []

        if self._current_agent:
            full_system_prompt = self._get_agent_manager().get_agent_system_prompt(
                self._current_agent, is_subagent_call=False
            )
        else:
            full_system_prompt = self._get_agent_manager().get_unified_system_prompt()

        prompt_parts = [full_system_prompt]
        
        # 性能优化：时间放最后，避免每次都重复构建
        time_part = f"# 当前系统时间\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        enabled_skills = Settings.get_instance().llm_enabled_skills.value
        if enabled_skills and self._agent_manager:
            skills_content = self._agent_manager.get_enabled_skills_content(
                enabled_skills
            )
            if skills_content:
                prompt_parts.append(skills_content)
        custom_prompt = llm_config.get("系统提示", "").strip()
        if custom_prompt:
            prompt_parts.append(custom_prompt)
        
        # 最后添加时间
        prompt_parts.append(time_part)
        
        # 性能优化：使用单一 join 操作
        full_system_content = "\n\n".join(prompt_parts)

        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = full_system_content
        else:
            messages.append(
                {
                    "role": "system",
                    "content": full_system_content,
                }
            )

        session.system_prompt = full_system_content
        normalized_session_messages = consolidate_messages(
            session.get_context_messages()
        )
        latest_user_message = ""
        latest_user_timestamp = ""
        params = {}
        history_messages = normalized_session_messages
        if history_messages and history_messages[-1].get("role") == "user":
            latest_user_message = history_messages[-1].get("content", "")
            latest_user_timestamp = history_messages[-1].get("timestamp", "")
            params = history_messages[-1].get("params", {})
            history_messages = history_messages[:-1]

        if self._get_memory_context:
            try:
                memory_context = self._get_memory_context(latest_user_message)
            except TypeError:
                memory_context = self._get_memory_context()
            if memory_context:
                messages[0]["content"] = (
                    messages[0]["content"] + "\n\n" + memory_context
                )
        budget = self._compactor.get_budget(llm_config)
        history_for_api, compaction_state, compaction_cache = anyio.run(
            anyio.to_thread.run_sync,
            lambda: self._compactor.compact(
                history_messages,
                budget,
                existing_cache=getattr(session, "compaction_cache", None),
                allow_llm_summary=allow_llm_summary,
            )
        )
        session.set_compaction_state(compaction_state)
        session.set_compaction_cache(compaction_cache)

        filtered_history = [m for m in history_for_api if m.get("role") != "system"]
        messages.extend(filtered_history)

        user_msg = {"role": "user", "content": latest_user_message, "params": params}
        if latest_user_timestamp:
            user_msg["timestamp"] = latest_user_timestamp
        messages.append(user_msg)
        return messages

    def _get_context_budget(self, llm_config: Dict) -> int:
        profile = get_provider_profile(llm_config)
        context_limit = int(profile.get("context_limit", 128000))

        # 支持多种上下文长度配置字段名
        for key in (
            "context_limit",
            "context_window",
            "max_context_tokens",
            "max_input_tokens",
            "最大Token",  # 用户在服务商设置中配置的上下文长度
        ):
            value = llm_config.get(key)
            if value in (None, ""):
                continue
            try:
                context_limit = int(value)
                break
            except Exception:
                continue

        max_output_tokens = llm_config.get(
            "最大新Token",
            llm_config.get(
                "max_tokens",
                llm_config.get(
                    "max_output_tokens", profile.get("max_output_tokens", 4096)
                ),
            ),
        )
        try:
            max_output_tokens = int(max_output_tokens)
        except Exception:
            max_output_tokens = int(profile.get("max_output_tokens", 4096))

        model_name = str(llm_config.get("model", "")).lower()
        profile_max_output = int(profile.get("max_output_tokens", 4096))

        if max_output_tokens > profile_max_output * 2:
            context_limit = min(context_limit, max_output_tokens)

        reserved = min(800, max_output_tokens)
        if "o1" in model_name or "o3" in model_name:
            reserved = min(max_output_tokens, 32000)

        return max(500, context_limit - reserved)

    def get_context_usage_snapshot(
        self, session: Optional[ChatSession] = None, llm_config: Optional[Dict] = None
    ) -> Dict[str, int]:
        session = session or self._session_manager.get_current_session()
        llm_config = llm_config or self._get_model_config()
        if not session or not llm_config:
            return {
                "used_tokens": 0,
                "budget_tokens": 0,
                "percent": 0,
                "compaction": self._compactor._make_state(),
                "normal_tokens": 0,
                "compacted_tokens": 0,
            }

        messages = self._build_messages(session, llm_config)
        budget_tokens = max(1, self._get_context_budget(llm_config))
        used_tokens = count_messages_tokens(messages)
        percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))
        
        # 计算普通上下文和压缩上下文的 token 分解
        compaction = dict(getattr(session, "compaction_state", {}) or {})
        normal_tokens = used_tokens
        compacted_tokens = 0
        
        if compaction.get("active"):
            # 有压缩时，从 compaction_cache 获取摘要消息的 token 数
            compaction_cache = getattr(session, "compaction_cache", {}) or {}
            summary_msg = compaction_cache.get("summary_message")
            if summary_msg:
                compacted_tokens = count_messages_tokens([summary_msg])
                normal_tokens = used_tokens - compacted_tokens
            else:
                # 如果没有缓存的摘要消息，基于比例估算
                summarized_count = compaction.get("summarized_count", 0)
                kept_count = compaction.get("kept_count", 0)
                total_count = summarized_count + kept_count
                if total_count > 0:
                    compacted_tokens = int(used_tokens * summarized_count / total_count * 0.3)  # 压缩后约为原来的30%
                    normal_tokens = used_tokens - compacted_tokens
        
        return {
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
            "percent": percent,
            "compaction": compaction,
            "normal_tokens": normal_tokens,
            "compacted_tokens": compacted_tokens,
        }

    def _start_worker(
        self,
        messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict],
    ):
        self.cleanup_worker()
        
        compaction_prompt = ""
        compaction_config = {}
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            compaction_prompt = self._agent_manager.get_agent_system_prompt(
                "compaction"
            )
            compaction_config = self._agent_manager.get_agent_config("compaction")
        session = self._session_manager.get_current_session()

        self._current_worker = OpenAIChatWorker(
            messages=messages,
            session_messages=session.get_context_messages() if session else [],
            llm_config=llm_config,
            tools=tools,
            tool_executor=self._tool_executor,
            tool_start_callback=self._callbacks.get("tool_call_sync_requested"),
            permission_check_callback=self._check_tool_permission,
            compaction_prompt=compaction_prompt,
            compaction_config=compaction_config,
            permission_cache=self._permission_cache,
            compactor=self._compactor,
            initial_compaction_cache=getattr(session, "compaction_cache", None),
        )
        # 无需同步缓存，因为共享同一个 PermissionCache 实例

        # API 模式：直接调用回调（不使用 Qt 信号-槽，避免跨线程事件循环问题）
        # API 模式下 worker 运行在没有 Qt 事件循环的线程中，Qt 信号无法传递
        if self._api_mode and self._worker_callbacks:
            self._current_worker.set_direct_callbacks(self._worker_callbacks)
        else:
            # UI 模式：使用 EventBus + QtSignalAdapter 机制
            # 阶段3迁移：不再直接 connect PyQt Signal，改用适配器
            self._setup_event_bus_adapter()

        self._current_worker.start()
        
        # API 模式：engine 也需要在主线程发射事件（用于 stream_started）
        if self._api_mode:
            import threading
            def emit_later():
                import time
                time.sleep(0.1)
                self._emit("stream_started")
            threading.Thread(target=emit_later, daemon=True).start()

    def _setup_event_bus_adapter(self):
        """
        设置事件总线适配器（UI 模式专用）
        
        阶段3迁移：不再直接 connect PyQt Signal，改用 QtSignalAdapter
        将 WorkerEventBus 的事件转发为 PyQt 信号，供 UI 层订阅。
        """
        if self._current_worker is None:
            return
        
        event_bus = self._current_worker.event_bus
        if event_bus is None:
            logger.warning("[ChatEngine] Worker has no event_bus")
            return
        
        # 清理旧的适配器
        if self._qt_signal_adapter is not None:
            self._qt_signal_adapter.cleanup()
        
        # 创建新的适配器
        self._qt_signal_adapter = QtSignalAdapter(event_bus, self._current_worker)
        self._qt_signal_adapter.setup_ui_signals()
        
        logger.debug("[ChatEngine] EventBus adapter setup complete")

    def _on_content_received(self, content_piece: str):
        self._emit("content_received", content_piece)

    def _on_reasoning_content_received(self, reasoning_piece: str):
        """DeepSeek 思考内容接收"""
        self._emit("reasoning_content_received", reasoning_piece)

    def _on_reasoning_finished(self):
        """DeepSeek 思考内容结束"""
        self._emit("reasoning_finished")

    def _on_tool_call_started(
        self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str
    ):
        self._emit("tool_call_started", tool_call_id, tool_name, arguments, round_id)

    def _on_question_asked(
        self, tool_call_id: str, question: str, options: list, multiple: bool
    ):
        self._emit("question_asked", tool_call_id, question, options, multiple)

    def _on_tool_result_received(
        self, tool_call_id: str, tool_name: str, arguments: dict, result: Any
    ):
        self._emit("tool_result_received", tool_call_id, tool_name, arguments, result)

    def _on_worker_finished(self, response: str):
        self._is_streaming = False
        self._emit("stream_finished", response)
        # 对话结束后清理 worker，释放内存
        self.cleanup_worker()

    def _on_worker_messages_updated(self, messages: List[Dict]):
        self._emit("messages_updated", consolidate_messages(messages or []))

    def _on_error(self, error: str):
        self._is_streaming = False
        self._emit("error", error)

    def _on_compaction_status_changed(self, status: dict):
        """压缩状态变化处理（阶段1修复：之前遗漏了这个信号连接）"""
        self._emit("compaction_status_changed", status)

    def stop(self) -> List[Dict]:
        
        worker = self._current_worker
        self._current_worker = None
        self._is_streaming = False
        interrupted_messages: List[Dict] = []

        if worker:
            try:
                interrupted_messages = worker.get_interrupted_messages()
            except Exception as exc:
                logger.warning(
                    f"[ChatEngine] Failed to snapshot interrupted messages: {exc}"
                )
            worker.cancel()
            if worker.isRunning():
                worker.quit()
            # 彻底清理 worker 的所有缓存数据
            try:
                worker.cleanup()
            except Exception as exc:
                logger.warning(f"[ChatEngine] Failed to cleanup worker: {exc}")

        return interrupted_messages
    
    def cleanup_worker(self):
        """
        清理当前 worker，释放所有缓存。
        应该在对话结束后或切换会话时调用。
        """
        # 清理事件总线适配器
        if self._qt_signal_adapter is not None:
            self._qt_signal_adapter.cleanup()
            self._qt_signal_adapter = None
        
        worker = self._current_worker
        self._current_worker = None
        self._is_streaming = False
        
        if worker:
            try:
                worker.cancel()
                if worker.isRunning():
                    worker.quit()
                worker.cleanup()
            except Exception as exc:
                logger.warning(f"[ChatEngine] Failed to cleanup worker: {exc}")
        
        # 清理 HTTP 客户端缓存
        self._compaction_http_client = None
        self._compaction_cache_config = None

    def provide_question_answer(self, answer: str):
        if self._current_worker and hasattr(self._current_worker, "provide_answer"):
            self._current_worker.provide_answer(answer)
