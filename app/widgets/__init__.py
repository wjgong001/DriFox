# -*- coding: utf-8 -*-
"""
llm_chatter widgets - 大模型对话框 UI 组件
"""

# 核心卡片
from app.widgets.base_settings_card import BaseSettingsCard
from app.widgets.llm_settings_card import LLMSettingsCard
from app.widgets.history_card import HistoryCard, get_message_preview
from app.widgets.message_card import MessageCard, create_welcome_card
from app.widgets.model_config_card import ModelConfigCard
from app.widgets.model_selector_popup import ModelSelectorPopup

# 悬浮组件
from app.widgets.tool_floating_widget import ToolFloatingWidget
from app.widgets.sub_agent_floating_widget import SubAgentFloatingWidget
from app.widgets.todo_floating_widget import TodoFloatingWidget
from app.widgets.question_floating_widget import QuestionFloatingWidget
from app.widgets.task_floating_widget import TaskFloatingWidget

# 对话组件
from app.widgets.bottom_input_area import SendableTextEdit
from app.widgets.context_usage_ring import ContextUsageRing
from app.widgets.conversation_node_preview import ConversationNodePreview
from app.widgets.memory_card import MemoryCardContent
from app.widgets.provider_edit_card import ProviderEditCard

# 对话框
from app.widgets.file_undo_dialog import FileUndoPreviewDialog

__all__ = [
    # 核心卡片
    "BaseSettingsCard",
    "LLMSettingsCard",
    "HistoryCard",
    "get_message_preview",
    "MessageCard",
    "create_welcome_card",
    "ModelConfigCard",
    "ModelSelectorPopup",
    # 悬浮组件
    "ToolFloatingWidget",
    "SubAgentFloatingWidget",
    "TodoFloatingWidget",
    "QuestionFloatingWidget",
    "TaskFloatingWidget",
    # 对话组件
    "SendableTextEdit",
    "ContextUsageRing",
    "ConversationNodePreview",
    "MemoryCardContent",
    # 对话框
    "FileUndoPreviewDialog",
]
