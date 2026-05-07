# -*- coding: utf-8 -*-
"""
聊天引擎模块 - 处理 LLM 对话的核心逻辑
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

from loguru import logger
from openai import OpenAI

from app.core.provider_profile import (
    get_provider_profile,
)
from app.tools import get_builtin_tools_schema
from app.core.chat_session import (
    ChatSession,
    SessionManager,
)
from app.utils.config import Settings
from app.core.message_content import (
    consolidate_messages,
    content_to_text,
)
from app.core.retry_helper import (
    create_api_call_with_retry,
)
from app.core.token_estimator import (
    estimate_tokens,
    count_messages_tokens,
)
from app.core.workers import OpenAIChatWorker

MAX_HISTORY_SNIPPET_CHARS = 1200
RECENT_HISTORY_MIN_MESSAGES = 6


def estimate_tokens_from_messages(messages: List[Dict]) -> int:
    """
    计算消息列表的 token 总数 (向后兼容接口)
    """
    if not messages:
        return 0
    return count_messages_tokens(messages)


def _normalize_message_content(content: Any) -> str:
    return content_to_text(content).strip()


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
        self._current_agent: Optional[str] = "plan"
        
        # API 模式专用：直接回调（绕过 Qt 信号-槽，避免跨线程事件循环问题）
        self._worker_callbacks = worker_callbacks or {}
        self._api_mode = api_mode
        
        # ========== 性能优化：HTTP 客户端和配置缓存 ==========
        self._compaction_http_client: Optional[OpenAI] = None  # 压缩摘要专用客户端
        self._compaction_cache_config: Optional[str] = None  # 缓存配置标识
        
        # 会话级权限缓存（跨 worker 持久化）
        self._session_permission_cache: Dict[str, bool] = {}
        
        # 事件总线（可选，用于发布事件）
        self._event_bus = None
    
    def set_event_bus(self, event_bus):
        """设置事件总线"""
        self._event_bus = event_bus

    def _make_compaction_state(
        self,
        active: bool = False,
        source: str = "history",
        kind: str = "",
        original_count: int = 0,
        summarized_count: int = 0,
        kept_count: int = 0,
        summary_count: int = 0,
        note: str = "",
    ) -> Dict[str, Any]:
        return {
            "active": bool(active),
            "source": source,
            "kind": kind,
            "original_count": int(original_count or 0),
            "summarized_count": int(summarized_count or 0),
            "kept_count": int(kept_count or 0),
            "summary_count": int(summary_count or 0),
            "note": note or "",
        }

    def _make_compaction_cache(
        self,
        active: bool = False,
        kind: str = "",
        cutoff_index: int = 0,
        source_message_count: int = 0,
        summarized_count: int = 0,
        tail_count: int = 0,
        budget_tokens: int = 0,
        summary_message: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "active": bool(active),
            "kind": kind or "",
            "cutoff_index": int(cutoff_index or 0),
            "source_message_count": int(source_message_count or 0),
            "summarized_count": int(summarized_count or 0),
            "tail_count": int(tail_count or 0),
            "budget_tokens": int(budget_tokens or 0),
            "summary_message": dict(summary_message)
            if isinstance(summary_message, dict)
            else None,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if active
            else "",
        }

    def _get_compaction_soft_limit(self, history_budget: int) -> int:
        return max(1, int(history_budget * 0.7))

    def _get_compaction_target_limit(self, history_budget: int) -> int:
        return max(1, int(history_budget * 0.5))

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
            # 先检查会话级缓存（ChatEngine 级别，跨 worker 持久化）
            if tool_name in self._session_permission_cache:
                logger.info(f"[_check_tool_permission] 使用 ChatEngine 会话缓存: tool={tool_name}")
                return "allow"
            
            from app.core.agent import (
                PermissionResolver,
            )

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
        if self._current_worker:
            self._current_worker.approve_permission(tool_call_id, auto_allow, session_allow)
        # 同步更新 ChatEngine 级别的会话缓存
        if session_allow and self._current_worker:
            tool_name = self._current_worker._permission_pending.get("tool_name", "") if self._current_worker._permission_pending else ""
            if tool_name:
                self._session_permission_cache[tool_name] = True
                from loguru import logger
                logger.info(f"[ChatEngine] 设置会话缓存: tool={tool_name}")

    def deny_tool_permission(self, tool_call_id: str):
        if self._current_worker:
            self._current_worker.deny_permission(tool_call_id)

    def clear_session_permission_cache(self, tool_name: str = None):
        """清除会话级权限缓存（ChatEngine 和 Worker 级别）"""
        # 清理 ChatEngine 级别的缓存
        if tool_name:
            if tool_name in self._session_permission_cache:
                del self._session_permission_cache[tool_name]
        else:
            self._session_permission_cache = {}
        
        # 清理 Worker 级别的缓存
        if self._current_worker:
            if tool_name:
                self._current_worker.set_session_permission_cache(tool_name, False)
            else:
                self._current_worker._session_permission_cache = {}

    def _truncate_with_head_tail(self, content: str, max_length: int) -> str:
        """
        首尾保留截断：保留内容开头和结尾，中间截断。
        适用于工具返回等长内容，保留关键结果。
        """
        if len(content) <= max_length:
            return content

        # 头尾各保留 40%，中间截断
        head_length = int(max_length * 0.4)
        tail_length = int(max_length * 0.4)

        # 确保头尾不重叠
        if head_length + tail_length >= max_length:
            return content[:max_length]

        head = content[:head_length]
        tail = content[-tail_length:]
        return f"{head}...[{len(content)}字截断]...{tail}"

    def _adaptive_truncate_content(
        self,
        content: str,
        position: int,
        total: int,
        target_total_length: Optional[int] = None,
        min_keep_ratio: float = 0.2,
        max_keep_ratio: float = 0.7,
        max_keep_length: int = 1000,
        content_ratios: Optional[List[float]] = None,
    ) -> str:
        """
        根据消息位置自适应截断内容。

        遗忘曲线模型：越久远的消息截断越多，保留越少；
        越近的消息截断越少，保留越多。

        Args:
            content: 原始内容
            position: 消息在列表中的索引（0=最旧）
            total: 消息总数
            target_total_length: 目标总长度（上下文限制的60%-70%）
            min_keep_ratio: 最旧消息的最小保留比例（默认15%）
            max_keep_ratio: 最新消息的最大保留比例（默认65%）
            max_keep_length: 最大保留长度（默认1000，防止工具结果过长）
            content_ratios: 预计算的内容比例列表（用于整体调整）

        Returns:
            截断后的内容，保留首尾关键信息
        """
        content_len = len(content)

        # 如果内容本身就小于等于 max_keep_length，不截断
        if content_len <= max_keep_length:
            return content

        keep_length: int

        # 自适应模式：根据目标总长度分配每条消息的配额
        # 每条消息的配额需要根据位置和内容比例调整

        # 计算基础配额
        base_quota = target_total_length / total

        # 根据位置计算权重（遗忘曲线，指数衰减）
        position_ratio = position / max(total - 1, 1)
        # 使用平方根让曲线更平滑，避免极端值
        weight = min_keep_ratio + (max_keep_ratio - min_keep_ratio) * (position_ratio ** 0.5)

        # 计算该消息的目标配额
        target_quota = base_quota * weight

        # 如果有内容比例参考，进行调整
        if content_ratios and len(content_ratios) == total:
            # 长内容多分配，短内容少分配（按比例）
            msg_ratio = content_ratios[position]
            avg_ratio = 1.0 / total
            # 调整系数：长内容适当多给，短内容适当少给
            ratio_factor = 0.5 + 0.5 * (msg_ratio / max(avg_ratio, 0.001))
            target_quota *= ratio_factor
        keep_length = int(target_quota)

        # 应用 max_keep_length 限制
        keep_length = min(keep_length, max_keep_length)

        # 最少保留 20 字符
        keep_length = max(keep_length, 20)

        # 如果计算出的保留长度大于等于内容长度，不截断
        if keep_length >= content_len:
            return content

        # 首尾保留策略：保留前40%和后40%
        head_ratio = 0.4
        head_length = int(keep_length * head_ratio)
        tail_length = int(keep_length * head_ratio)

        head = content[:head_length]
        tail = content[-tail_length:] if tail_length > 0 else ""

        return f"{head}...[{keep_length}字]...{tail}"

    def _summarize_compacted_messages(
        self,
        messages: List[Dict[str, str]],
        allow_llm_summary: bool = False,
        history_budget: Optional[int] = None,
    ) -> str:
        if not messages:
            return ""

        llm_summary = ""
        if allow_llm_summary:
            llm_summary = self._summarize_compacted_messages_with_agent(messages)
        if llm_summary:
            return llm_summary

        summary_lines = [
            "## Earlier Conversation Summary",
            "以下是为节省上下文窗口而压缩的较早对话，请把它当作已确认的历史上下文继续工作。",
        ]

        total_messages = len(messages)

        # 预计算每条消息内容的长度比例，用于智能分配配额
        contents = []
        for msg in messages:
            content = _normalize_message_content(msg.get("content", ""))
            if not content:
                content = ""
            single_line = " ".join(content.split())
            contents.append(single_line)

        total_content_length = sum(len(c) for c in contents)
        content_ratios = [
            len(c) / total_content_length if total_content_length > 0 else 1 / total_messages
            for c in contents
        ]

        # 计算目标总长度（上下文限制的60%-70%）
        target_total_length: Optional[int] = None
        if history_budget is not None and history_budget > 0:
            target_total_length = int(history_budget * 0.6)

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            content = contents[idx] if idx < len(contents) else ""

            # 自适应截断：越旧的消息截断越多
            content = self._adaptive_truncate_content(
                content,
                position=idx,
                total=total_messages,
                target_total_length=target_total_length,
                content_ratios=content_ratios if total_content_length > 1000 else None,
            )

            if role == "user":
                summary_lines.append("# User")
                summary_lines.append(f"{content}")
            elif role == "assistant":
                summary_lines.append("# Assistant")
                summary_lines.append(f"{content}")
            elif role == "tool":
                tool_name = msg.get("name", "")
                summary_lines.append("# Tool")
                args = self._adaptive_truncate_content(
                    json.dumps(msg.get("arguments", "")),
                    position=idx,
                    total=total_messages,
                    target_total_length=target_total_length,
                    content_ratios=content_ratios if total_content_length > 1000 else None,
                )
                summary_lines.append(f"{tool_name}:\n args:{args}\nresult:{content}")

        summary_lines.append(
            "### Compression Note\n如果后续细节与当前上下文冲突，以最近保留的原始消息和最新任务状态为准。"
        )
        return "\n".join(summary_lines)

    def _build_compaction_agent_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        transcript_lines = []
        for msg in messages or []:
            role = msg.get("role", "unknown")
            content = _normalize_message_content(msg.get("content", ""))
            if not content:
                continue
            single_line = " ".join(content.split())
            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
                transcript_lines.append(f"[tool:{tool_name}] {single_line[:1200]}")
            else:
                transcript_lines.append(f"[{role}] {single_line[:1200]}")

        prompt = (
            "请压缩下面的较早对话，使后续模型可以继续当前编码任务。\n\n"
            "输出要求：\n"
            "1. 使用 Markdown。\n"
            "2. 优先保留：任务目标、已完成工作、关键文件/模块、关键工具结果、当前剩余问题。\n"
            "3. 不要只重复用户原始提问。\n"
            "4. 删除寒暄、重复探索、低价值调试细节。\n"
            "5. 如果信息不足，不要编造。\n\n"
            "【待压缩对话】\n" + "\n".join(transcript_lines)
        )

        system_prompt = ""
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            system_prompt = self._agent_manager.get_agent_system_prompt("compaction")
        if not system_prompt:
            system_prompt = (
                "你是一个上下文压缩专家，负责提炼后续继续执行编码任务所需的摘要。"
            )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    def _summarize_compacted_messages_with_agent(
        self, messages: List[Dict[str, Any]]
    ) -> str:
        if not messages:
            return ""

        llm_config = self._get_model_config() or {}
        api_key = str(llm_config.get("API_KEY", "") or "").strip()
        base_url = llm_config.get("API_URL") or None
        auth_type = llm_config.get("认证方式", "bearer")
        if not api_key and auth_type != "none":
            return ""

        compaction_config = {}
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            compaction_config = self._agent_manager.get_agent_config("compaction")

        model = str(
            compaction_config.get("model") or llm_config.get("模型名称", "gpt-4o")
        )
        
        # 性能优化：复用 HTTP 客户端
        client = self._get_compaction_http_client(api_key, base_url, auth_type)

        req_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": self._build_compaction_agent_messages(messages),
            "stream": False,
            "max_tokens": 1200,
        }
        temperature = compaction_config.get("temperature")
        if temperature is not None:
            req_kwargs["temperature"] = temperature
        top_p = compaction_config.get("top_p")
        if top_p is not None:
            req_kwargs["top_p"] = top_p

        try:

            def create_task():
                return client.chat.completions.create(**req_kwargs)

            resp = create_api_call_with_retry(client, create_task)
            content = (resp.choices[0].message.content or "").strip()
            return content
        except Exception as exc:
            logger.warning(
                f"[Compaction] Agent summarization failed, fallback to heuristic: {exc}"
            )
            return ""
    
    def _get_compaction_http_client(self, api_key: str, base_url: str, auth_type: str) -> OpenAI:
        """获取或创建压缩摘要专用的 HTTP 客户端（复用）"""
        config_key = f"{auth_type}:{api_key[:8]}:{base_url}"
        
        if (self._compaction_http_client is not None and 
            self._compaction_cache_config == config_key):
            return self._compaction_http_client
        
        self._compaction_http_client = OpenAI(
            api_key=api_key if api_key and auth_type != "none" else "dummy",
            base_url=base_url,
            timeout=60.0,
        )
        self._compaction_cache_config = config_key
        return self._compaction_http_client

    def _has_structured_tool_history(
        self, history_messages: List[Dict[str, Any]]
    ) -> bool:
        for msg in history_messages:
            if msg.get("role") == "tool":
                return True
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                return True
        return False

    def _compact_structured_history_messages(
        self,
        history_messages: List[Dict[str, Any]],
        history_budget: int,
        allow_llm_summary: bool = False,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        if not history_messages or history_budget <= 0:
            return [], self._make_compaction_state(), self._make_compaction_cache()

        soft_limit = self._get_compaction_soft_limit(history_budget)
        target_limit = self._get_compaction_target_limit(history_budget)

        if estimate_tokens_from_messages(history_messages) <= soft_limit:
            return (
                history_messages,
                self._make_compaction_state(
                    original_count=len(history_messages),
                    kept_count=len(history_messages),
                ),
                self._make_compaction_cache(),
            )

        # 从后向前收集 recent_messages，确保不拆分 tool_calls 配对
        recent_messages: List[Dict[str, Any]] = []
        recent_tokens = 0

        # 跟踪当前未完成的 tool 响应（assistant 的 tool_result）
        pending_tool_results: set = set()

        i = len(history_messages) - 1
        while i >= 0:
            msg = history_messages[i]
            msg_tokens = estimate_tokens_from_messages([msg])
            role = msg.get("role")

            # 构建当前消息的完整配对集合
            if role == "assistant":
                # 收集这个 assistant 消息发起的 tool_calls
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("id") in pending_tool_results:
                        pending_tool_results.discard(tc.get("id"))

            elif role == "tool":
                pending_tool_results.add(msg.get("tool_call_id"))

            # 把消息加入 recent（从后向前插入）
            recent_messages.insert(0, msg)
            recent_tokens += msg_tokens

            # 检查 token 限制
            token_exceeded = (
                recent_messages
                and recent_tokens + msg_tokens > target_limit
            )

            # 如果 token 超限且不会拆分配对，则停止
            if token_exceeded and not pending_tool_results:
                break

            i -= 1

        if len(recent_messages) == len(history_messages):
            return (
                recent_messages,
                self._make_compaction_state(
                    original_count=len(history_messages),
                    kept_count=len(recent_messages),
                ),
                self._make_compaction_cache(),
            )

        compacted = history_messages[: len(history_messages) - len(recent_messages)]

        # 计算 summary 可用的空间 = budget - recent_tokens - summary_message 开销
        summary_budget = history_budget - recent_tokens - 500  # 500 是基础开销

        compact_summary = self._summarize_compacted_messages(
            compacted, allow_llm_summary=allow_llm_summary, history_budget=summary_budget
        )
        if not compact_summary:
            return (
                recent_messages,
                self._make_compaction_state(
                    original_count=len(history_messages),
                    kept_count=len(recent_messages),
                ),
                self._make_compaction_cache(),
            )

        summary_message = {"role": "assistant", "content": compact_summary}
        result_messages = [summary_message] + recent_messages

        return (
            result_messages,
            self._make_compaction_state(
                active=True,
                source="history",
                kind="structured",
                original_count=len(history_messages),
                summarized_count=len(compacted),
                kept_count=len(recent_messages),
                summary_count=estimate_tokens_from_messages(compacted) - estimate_tokens_from_messages([summary_message]),
                note=f"已压缩 {len(compacted)} 条含工具历史消息",
            ),
            self._make_compaction_cache(
                active=True,
                kind="structured",
                cutoff_index=len(compacted),
                source_message_count=len(history_messages),
                summarized_count=len(compacted),
                tail_count=len(recent_messages),
                budget_tokens=history_budget,
                summary_message=summary_message,
            ),
        )

    def _compact_history_messages(
        self,
        history_messages: List[Dict[str, Any]],
        history_budget: int,
        existing_cache: Optional[Dict[str, Any]] = None,
        allow_llm_summary: bool = False,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        if not history_messages or history_budget <= 0:
            return [], self._make_compaction_state(), self._make_compaction_cache()

        normalized = consolidate_messages(history_messages)
        soft_limit = self._get_compaction_soft_limit(history_budget)
        target_limit = self._get_compaction_target_limit(history_budget)

        if not normalized:
            return [], self._make_compaction_state(), self._make_compaction_cache()

        if estimate_tokens_from_messages(normalized) <= soft_limit:
            return (
                normalized,
                self._make_compaction_state(
                    original_count=len(normalized),
                    kept_count=len(normalized),
                ),
                self._make_compaction_cache(),
            )

        cached = existing_cache or {}
        if cached.get("active") and cached.get("summary_message"):
            cutoff_index = int(cached.get("cutoff_index", 0) or 0)
            if 0 < cutoff_index <= len(normalized):
                cached_messages = [
                    cached.get("summary_message"),
                    *normalized[cutoff_index:],
                ]
                if estimate_tokens_from_messages(cached_messages) <= soft_limit:
                    summarized_count = int(
                        cached.get("summarized_count", cutoff_index) or cutoff_index
                    )
                    tail_count = len(normalized) - cutoff_index
                    return (
                        cached_messages,
                        self._make_compaction_state(
                            active=True,
                            source="history",
                            kind=str(cached.get("kind", "plain") or "plain"),
                            original_count=len(normalized),
                            summarized_count=summarized_count,
                            kept_count=tail_count,
                            summary_count=1,
                            note=f"复用已压缩摘要，覆盖 {summarized_count} 条较早消息",
                        ),
                        self._make_compaction_cache(
                            active=True,
                            kind=str(cached.get("kind", "plain") or "plain"),
                            cutoff_index=cutoff_index,
                            source_message_count=len(normalized),
                            summarized_count=summarized_count,
                            tail_count=tail_count,
                            budget_tokens=history_budget,
                            summary_message=cached.get("summary_message"),
                        ),
                    )

        if self._has_structured_tool_history(normalized):
            return self._compact_structured_history_messages(
                normalized,
                history_budget,
                allow_llm_summary=allow_llm_summary,
            )

        recent_messages: List[Dict[str, str]] = []
        recent_tokens = 0
        min_recent_tokens = int(target_limit * 0.85)
        for msg in reversed(normalized):
            msg_tokens = estimate_tokens_from_messages([msg])
            if recent_messages and recent_tokens + msg_tokens > target_limit:
                break
            recent_messages.insert(0, msg)
            recent_tokens += msg_tokens
            if (
                len(recent_messages) >= RECENT_HISTORY_MIN_MESSAGES
                and recent_tokens >= min_recent_tokens
            ):
                break

        if len(recent_messages) == len(normalized):
            return (
                recent_messages,
                self._make_compaction_state(
                    original_count=len(normalized),
                    kept_count=len(recent_messages),
                ),
                self._make_compaction_cache(),
            )

        compacted = normalized[: len(normalized) - len(recent_messages)]

        # 计算 summary 可用的空间 = budget - recent_tokens - summary_message 开销
        summary_budget = history_budget - recent_tokens - 500

        compact_summary = self._summarize_compacted_messages(
            compacted, allow_llm_summary=allow_llm_summary, history_budget=summary_budget
        )
        if not compact_summary:
            return (
                recent_messages,
                self._make_compaction_state(
                    original_count=len(normalized),
                    kept_count=len(recent_messages),
                ),
                self._make_compaction_cache(),
            )

        summary_message = {
            "role": "assistant",
            "content": compact_summary,
        }
        result_messages = [summary_message] + recent_messages

        while (
            len(result_messages) > 1
            and estimate_tokens_from_messages(result_messages) > target_limit
        ):
            if len(recent_messages) > 1:
                recent_messages.pop(0)
                result_messages = [summary_message] + recent_messages
                continue

            result_messages = [summary_message]
            break
        return (
            result_messages,
            self._make_compaction_state(
                active=True,
                source="history",
                kind="plain",
                original_count=len(normalized),
                summarized_count=len(compacted),
                kept_count=len(recent_messages),
                summary_count=estimate_tokens_from_messages(compacted) - estimate_tokens_from_messages([summary_message]),
                note=f"已压缩 {len(compacted)} 条较早消息",
            ),
            self._make_compaction_cache(
                active=True,
                kind="plain",
                cutoff_index=len(compacted),
                source_message_count=len(normalized),
                summarized_count=len(compacted),
                tail_count=len(recent_messages),
                budget_tokens=history_budget,
                summary_message=summary_message,
            ),
        )

    def set_callback(self, event: str, callback: Callable):
        self._callbacks[event] = callback

    def _emit(self, event: str, *args, **kwargs):
        callback = self._callbacks.get(event)
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
        session = self._session_manager.get_current_session()

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

        max_context_tokens = self._get_context_budget(llm_config)
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
        available_history_budget = (
            max_context_tokens - estimate_tokens(latest_user_message) - 200
        )
        history_for_api, compaction_state, compaction_cache = (
            self._compact_history_messages(
                history_messages,
                available_history_budget,
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
                "compaction": self._make_compaction_state(),
            }

        messages = self._build_messages(session, llm_config)
        budget_tokens = max(1, self._get_context_budget(llm_config))
        used_tokens = estimate_tokens_from_messages(messages)
        percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))
        return {
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
            "percent": percent,
            "compaction": dict(getattr(session, "compaction_state", {}) or {}),
        }

    def _start_worker(
        self,
        messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict],
    ):
        # 启动新 worker 前，先彻底清理旧的 worker
        if self._current_worker:
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
        )

        # API 模式：直接调用回调（不使用 Qt 信号-槽，避免跨线程事件循环问题）
        # API 模式下 worker 运行在没有 Qt 事件循环的线程中，Qt 信号无法传递
        if self._api_mode and self._worker_callbacks:
            self._current_worker.set_direct_callbacks(self._worker_callbacks)
        else:
            # UI 模式：使用 Qt 信号-槽机制
            self._current_worker.content_received.connect(self._on_content_received)
            self._current_worker.reasoning_content_received.connect(self._on_reasoning_content_received)
            self._current_worker.tool_call_started.connect(self._on_tool_call_started)
            self._current_worker.tool_result_received.connect(self._on_tool_result_received)
            self._current_worker.error_occurred.connect(self._on_error)
            self._current_worker.finished_with_content.connect(self._on_worker_finished)
            self._current_worker.finished_with_messages.connect(
                self._on_worker_messages_updated
            )
            self._current_worker.question_asked.connect(self._on_question_asked)
            self._current_worker.permission_approval_requested.connect(
                self._on_permission_approval_requested
            )

        self._current_worker.start()
        self._emit("stream_started")

    def _on_content_received(self, content_piece: str):
        self._emit("content_received", content_piece)

    def _on_reasoning_content_received(self, reasoning_piece: str):
        """DeepSeek 思考内容接收"""
        self._emit("reasoning_content_received", reasoning_piece)

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
