# -*- coding: utf-8 -*-
"""
话题摘要任务 - 异步生成话题摘要和长期记忆判断
使用 threading 替代 QRunnable
"""

import json
import re
import threading
from typing import Callable, Optional, List, Dict, Any

from loguru import logger
from openai import OpenAI

from app.core.memory_manager import MEMORY_CATEGORIES
from app.core.retry_helper import create_api_call_with_retry


class TopicSummaryTask:
    """异步生成话题摘要任务 - 支持增量摘要和长期记忆判断
    
    使用 threading 替代 QRunnable，保持相同的 API 接口
    """

    def __init__(
        self,
        messages: list,
        llm_config: dict,
        callback: Callable[[Dict[str, Any]], None],
        previous_summary: str = None,
        long_term_memory: str = "",
        existing_memories: list = None,
    ):
        self.messages = messages
        self.llm_config = llm_config
        self.callback = callback
        self.previous_summary = previous_summary
        self.long_term_memory = long_term_memory
        self.existing_memories = existing_memories or []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动后台执行"""
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def _build_conversation_context(self) -> str:
        """构建包含更多历史的消息上下文"""
        # 取最近12条消息，覆盖更多对话历史
        user_only_msgs = [msg for msg in self.messages if msg.get("role") == "user"]
        lines = []
        for msg in user_only_msgs:
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                content = "\n".join(texts)
            # 截取前300字，避免过长
            truncated = content[:300] + ("..." if len(content) > 300 else "")
            lines.append(truncated)
        
        return "\n".join(lines)

    def _run(self) -> None:
        """执行摘要生成"""
        try:
            if not self.messages:
                self.callback({
                    "topic_summary": "",
                    "should_update_memory": False,
                    "memory_content": "",
                })
                return
            
            # 构建包含更多历史的消息上下文
            summary_text = self._build_conversation_context()

            existing_memories_text = ""
            if self.existing_memories:
                mem_lines = []
                for mem in self.existing_memories:
                    if isinstance(mem, dict):
                        content = mem.get("content", "")
                        enabled = mem.get("enabled", True)
                        if enabled:
                            mem_lines.append(f"- {content}")
                    elif isinstance(mem, str) and mem:
                        mem_lines.append(f"- {mem}")
                if mem_lines:
                    existing_memories_text = (
                        "\n【已有记忆】（请勿生成重复或相似内容）:\n"
                        + "\n".join(mem_lines)
                    )

            category_list = ";".join(
                f"{k}: {v.replace('【', '').replace('】', '')}"
                for k, v in MEMORY_CATEGORIES.items()
            )

            if self.previous_summary:
                prompt = (
                    "你是一个对话标题和长期记忆助手。\n"
                    "最近的用户对话历史：\n"
                    f"{summary_text}\n\n"
                    "根据对话生成标题，判断是否需要保存长期记忆。\n\n"
                    "【标题要求】\n"
                    "- 不超过15字，体现用户意图\n"
                    "- 如：\"生成PPT\"、\"调试bug\"、\"咨询问题\"\n\n"
                    "【标题更新原则】\n"
                    "你不应该仅根据最新一条消息就生成新标题。而是应该：\n"
                    "1. 如果已有标题反映的是本次对话的核心主题，即使最新消息是简单问候，也要保留原标题\n"
                    "2. 只有当对话主题发生实质性转变时才更新标题\n"
                    "3. 问候、寒暄、确认类消息不应该改变已有标题\n\n"
                    "【标题更新规则】\n"
                    "以下情况应该保留\"之前的标题\"：\n"
                    "- 最新消息是简单问候（你好、hi、hello、嗨、哈喽等）\n"
                    "- 最新消息是确认/同意（好的、嗯、是的、对、ok等）\n"
                    "- 最新消息是简短的感谢（谢谢、感谢等）\n"
                    "- 最新消息是要求继续（继续、接着说等）\n\n"
                    "以下情况可以更新标题：\n"
                    "- 对话主题发生实质性转变（从A话题切换到B话题）\n"
                    "- 用户明确表达了新的意图\n"
                    "- 对话已解决，正在开始全新的任务\n\n"
                    "【之前的标题】\n"
                    f"{self.previous_summary}\n\n"
                    f"{existing_memories_text}\n\n"
                    "输出JSON：\n"
                    "```json\n"
                    "{\n"
                    '  "topic_summary": "简短标题（≤15字）",\n'
                    '  "should_update_memory": true/false,\n'
                    '  "memory_content": "记忆内容（should_update=true时填写）",\n'
                    f'  "memory_category": "{category_list}",\n'
                    '  "hit_memories": ["本轮引用的已有记忆"]\n'
                    "}\n"
                    "```"
                )
            else:
                prompt = (
                    "你是一个对话标题和长期记忆助手。\n"
                    "最近的用户对话历史：\n"
                    f"{summary_text}\n\n"
                    "根据对话生成标题，判断是否需要保存长期记忆。\n\n"
                    "【标题要求】\n"
                    "- 不超过15字，体现用户意图\n"
                    "- 如：\"生成PPT\"、\"调试bug\"、\"咨询问题\"\n\n"
                    "【长期记忆规则】\n"
                    "- 只有当对话包含对后续工作有价值的信息时才保存记忆\n"
                    "- 记忆内容应该简洁、具体、可操作\n"
                    "- 不要保存简单的问答、寒暄或一次性信息\n\n"
                    f"{existing_memories_text}\n\n"
                    "输出JSON：\n"
                    "```json\n"
                    "{\n"
                    '  "topic_summary": "简短标题（≤15字）",\n'
                    '  "should_update_memory": true/false,\n'
                    '  "memory_content": "记忆内容（should_update=true时填写）",\n'
                    f'  "memory_category": "{category_list}",\n'
                    '  "hit_memories": ["本轮引用的已有记忆"]\n'
                    "}\n"
                    "```"
                )
            
            client = OpenAI(
                api_key=self.llm_config.get("API_KEY", ""),
                base_url=self.llm_config.get("API_URL"),
            )

            def create_task():
                return client.chat.completions.create(
                    model=self.llm_config.get("模型名称", "gpt-4o"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=1000,
                )

            resp = create_api_call_with_retry(client, create_task)
            raw_response = resp.choices[0].message.content.strip()
            json_match = re.search(r"\{[^{}]*\}", raw_response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                callback_data = {
                    "topic_summary": result.get("topic_summary", ""),
                    "should_update_memory": result.get("should_update_memory", False),
                    "memory_content": result.get("memory_content", ""),
                    "memory_category": result.get("memory_category", "task_preference"),
                    "hit_memories": result.get("hit_memories", []),
                }
                self.callback(callback_data)
            else:
                self.callback({
                    "topic_summary": raw_response,
                    "should_update_memory": False,
                    "memory_content": "",
                    "memory_category": "task_preference",
                    "hit_memories": [],
                })
        except Exception as e:
            logger.exception(f"[OpenAI] 获取标题失败: {e}")
            # 适配错误回调格式
            try:
                self.callback({
                    "topic_summary": "",
                    "should_update_memory": False,
                    "memory_content": "",
                }, error=str(e))
            except TypeError:
                # 如果 callback 不支持 error 参数，只传一个参数
                self.callback({
                    "topic_summary": "",
                    "should_update_memory": False,
                    "memory_content": "",
                    "error": str(e),
                })