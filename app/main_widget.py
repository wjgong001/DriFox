# -*- coding: utf-8 -*-
import ctypes
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from PyQt5.QtCore import (
    QTimer,
    pyqtSignal,
    QThreadPool,
    Qt,
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QApplication,
    QWidget,
    QFileDialog, QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QButtonGroup, QFrame, QScrollArea,
)
from loguru import logger
from qfluentwidgets import (
    setFont,
    FluentIcon,
    SingleDirectionScrollArea,
    InfoBar,
    InfoBarPosition,
    TransparentToolButton, StrongBodyLabel,
)

from app.constants import (
    FREE_PROVIDERS,
    PROVIDER_ICONS,
    PROVIDER_MODELS,
)
from app.core import (
    ChatEngine,
    ToolExecutor,
    MemoryManagerCore,
)
from app.core.agent import AgentManager
from app.utils.chat_session import (
    SessionManager,
    ChatSession,
)
from app.utils.diff_viewer import (
    DiffHtmlGenerator,
    DiffViewerWindow,
)
from app.utils.history_manager import (
    HistoryManager,
)
from app.utils.message_content import (
    consolidate_messages,
    content_to_text,
    get_user_round_ranges,
    group_messages_for_display,
)
from app.utils.worker import (
    TopicSummaryTask,
)
from app.widgets.base_settings_card import (
    BaseSettingsCard,
)
from app.widgets.bottom_input_area import (
    SendableTextEdit,
)
from app.widgets.context_usage_ring import (
    ContextUsageRing,
)
from app.widgets.conversation_node_preview import (
    ConversationNodePreview,
)
from app.widgets.file_undo_dialog import (
    FileUndoPreviewDialog,
)
from app.widgets.history_card import (
    HistoryCard,
    get_message_preview,
)
from app.widgets.llm_settings_card import (
    LLMSettingsCard,
)
from app.widgets.memory_manager import (
    MemoryManagerDialog,
)
from app.widgets.message_card import (
    MessageCard,
    create_welcome_card,
)
from app.widgets.model_config_card import (
    ModelConfigCard,
)
from app.widgets.question_floating_widget import (
    QuestionFloatingWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition
from app.widgets.sub_agent_floating_widget import (
    SubAgentFloatingWidget,
)
from app.widgets.todo_floating_widget import (
    TodoFloatingWidget,
)
from app.widgets.tool_floating_widget import (
    ToolFloatingWidget,
)
from app.widgets.ui_helpers import *
from app.widgets.ui_helpers import add_message_to_layout, refresh_history_card_if_visible, \
    init_new_session_after_archive, clear_and_show_welcome, refresh_session_view, save_or_archive_session, \
    invalidate_session_card_cache, delete_widgets_from_layout, init_after_loading_session, setup_user_card_signals, \
    post_append_user_message, create_assistant_card_widget, scroll_to_bottom_if_streaming, \
    build_node_preview_from_session, calculate_scroll_progress, find_user_card_at_index, truncate_and_remove_round, \
    log_deletion_stats, restore_input_from_card, find_last_tool_call_id_after_round, get_first_file_operation, \
    show_diff_viewer, render_batch_to_assistant_card
from app.tool_window import (
    ToolWindow,
    DockPosition,
    DockCategory,
)
from app.utils.config import Settings
from app.utils.utils import get_icon, get_font_family_css


class OpenAIChatToolWindow(ToolWindow):
    name = "飘狐 DriFox"
    icon = get_icon("drifox")
    singleton = True
    default_position = DockPosition.TOP
    CATEGORIES = [DockCategory.PROJECT]
    display_order = 30
    session_manager = None
    _valid_configs: Dict[str, Dict[str, Any]] = {}
    history_manager = None
    _agent_manager: Optional[AgentManager] = None
    _current_agent: str = "build"
    _current_session_id: Optional[str] = None
    _settings_popup = None
    _is_welcome = False
    _is_searching: bool = False
    _search_results: List[int] = []
    _current_search_index: int = -1
    _chat_engine: Optional[ChatEngine] = None
    _tool_executor: Optional[ToolExecutor] = None
    _memory_manager: Optional[MemoryManagerCore] = None
    _is_continuing: bool = False
    _processed_tool_ids: set = set()
    _current_assistant_card = None
    _tool_call_depth: int = 0
    _pending_tool_calls: int = 0
    _first_tool_result: bool = True
    _tool_cancelled_by_user: bool = False
    _cancelled_tool_call_id: Optional[str] = None
    _todo_floating_widget = None
    _question_floating_widget = None
    _question_tool_call_id = None
    _window_active: bool = True
    _history_preview_messages: Optional[List[dict]] = None
    _history_preview_title: str = ""
    insertResponse = pyqtSignal(str)
    createResponse = pyqtSignal(str)
    contextActionRequested = pyqtSignal(str, str)
    skillExecutionRequested = pyqtSignal(str, dict)
    userInterventionRequested = pyqtSignal(dict)
    executionResultProduced = pyqtSignal(str)
    toolStartUiSyncRequested = pyqtSignal(str, str, object, str)

    def __init__(self, homepage, button):
        # 需要在 super().__init__() 之前初始化，因为 setup_ui() 会用到
        from app.core.agent import AgentManager
        self._agent_manager = AgentManager()
        
        super().__init__(homepage, button)
        self._session_card_cache: Dict[str, Dict[str, Any]] = {}
        self._current_history_project: Optional[str] = None  # 当前历史面板项目过滤
        self._welcome_card_cache: Dict[str, MessageCard] = {}
        self._displayed_session_id: Optional[str] = None
        self._initial_visible_batch_count = 12
        self._incremental_visible_batch_count = 8
        self._history_load_threshold = 48
        self._message_batch: List[List[Dict[str, Any]]] = []
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        self._suspend_auto_scroll = False
        self._gen_thread_pool = QThreadPool()
        self._gen_thread_pool.setMaxThreadCount(2)
        self._pending_scroll_to_bottom = False
        self._bottom_anchor_deadline = 0.0
        self._last_visible_user_pair_index = -1
        self._scroll_bottom_timer = QTimer(self)
        self._scroll_bottom_timer.setSingleShot(True)
        self._scroll_bottom_timer.setInterval(24)
        self._scroll_bottom_timer.timeout.connect(self._do_scroll_to_bottom)
        self._bottom_anchor_timer = QTimer(self)
        self._bottom_anchor_timer.setSingleShot(True)
        self._bottom_anchor_timer.setInterval(80)
        self._bottom_anchor_timer.timeout.connect(self._maintain_bottom_anchor)
        self._suppress_scroll_sync_count = 0  # 加载历史时抑制滚动同步的计数器
        # resize 防抖定时器 - 性能优化：增加防抖时间减少卡顿
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(30)  # 30ms 防抖，及时响应 resize
        self._resize_debounce_timer.timeout.connect(self._do_debounced_resize)
        # resize 完成后更新所有卡片的定时器（延迟更新非可见区域卡片）
        self._resize_complete_timer = QTimer(self)
        self._resize_complete_timer.setSingleShot(True)
        self._resize_complete_timer.setInterval(100)  # resize 结束后尽快恢复真实内容
        self._resize_complete_timer.timeout.connect(self._sync_all_cards_width)
        self._pending_resize_sync = False
        self._resize_preview_active = False
        self._last_chat_viewport_width = 0
        self._scroll_sync_timer = QTimer(self)
        self._scroll_sync_timer.setSingleShot(True)
        self._scroll_sync_timer.setInterval(80)
        self._scroll_sync_timer.timeout.connect(self._sync_visible_cards_on_scroll)
        self.toolStartUiSyncRequested.connect(
            self._handle_tool_start_ui_sync, type=Qt.BlockingQueuedConnection
        )
        self.homepage = homepage
        self._is_streaming = False
        homepage.installEventFilter(self)
        self._window_active = homepage.isActiveWindow()
        # 问题修复：初始化未定义的属性
        self._pending_permission_tool_call_id: Optional[str] = None
        self._question_tool_call_id: Optional[str] = None
        self._current_assistant_round_index: Optional[int] = None  # 跟踪当前应分配给 assistant 的 round_index
        self._pending_scroll_to_index: Optional[int] = None  # 时间线节点滚动目标索引
        self._pending_scroll_to_batch: Optional[int] = None  # 时间线节点滚动目标 batch 索引
        self._pending_scroll_to_update: Optional[int] = None  # 待更新的节点索引（用于同步高亮和进度）
        self.session_manager = SessionManager()
        self.session_manager.create_new_session()
        self._current_session_id = self.session_manager.get_current_session().session_id
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self._auto_save_current_session)
            except Exception:
                pass
        if hasattr(self.homepage, "global_variables_changed"):
            self.homepage.global_variables_changed.connect(self._load_model_configs)
        self._initialize_managers()

        # 设置文件操作记录的会话上下文
        if self._tool_executor:
            self._tool_executor.set_session_context(self._current_session_id)

    def _initialize_managers(self):
        """初始化核心管理器"""
        canvas_name = getattr(self.homepage, "workflow_name", "default") or "default"
        self._memory_manager = MemoryManagerCore(canvas_name)
        self._tool_executor = ToolExecutor(self.homepage, workdir=Path(__file__).parent.parent.parent)
        self._tool_executor.set_memory_manager(self._memory_manager)
        self._tool_executor.set_llm_config_getter(self._get_current_model_config)
        self._tool_executor.set_session_messages_getter(
            self._get_current_session_messages_for_tools
        )
        # _agent_manager 已在 __init__ 开头初始化，这里只记录日志
        all_agents = self._agent_manager.list_agents()
        primary_agents = self._agent_manager.list_primary_agents()
        logger.info(f"[Init] AgentManager 加载了 {len(all_agents)} 个智能体，其中 {len(primary_agents)} 个 primary")

        from app.core.sub_agent_executor import (
            SubAgentManager,
        )

        self._sub_agent_manager = SubAgentManager(
            agent_manager=self._agent_manager,
            tool_executor=self._tool_executor,
            get_llm_config=self._get_current_model_config,
        )
        self._sub_agent_manager.task_started.connect(self._on_sub_agent_task_started)
        self._sub_agent_manager.task_finished.connect(self._on_sub_agent_task_finished)
        self._tool_executor.set_sub_agent_manager(self._sub_agent_manager)

        self._chat_engine = ChatEngine(
            session_manager=self.session_manager,
            get_model_config=self._get_current_model_config,
            get_context_provider=lambda: None,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
            get_chat_cards=self._get_chat_cards_for_engine,
            get_memory_context=self._build_memory_context_for_engine,
        )

        self._chat_engine.set_callback("content_received", self._on_content_received)
        self._chat_engine.set_callback("reasoning_content_received", self._on_reasoning_content_received)
        self._chat_engine.set_callback("tool_call_started", self._on_tool_call_started)
        self._chat_engine.set_callback(
            "tool_call_sync_requested", self._request_tool_start_ui_sync
        )
        self._chat_engine.set_callback(
            "tool_result_received", self._on_tool_result_received
        )
        self._chat_engine.set_callback("stream_started", self._on_stream_started)
        self._chat_engine.set_callback("stream_finished", self._on_stream_finished)
        self._chat_engine.set_callback("messages_updated", self._on_messages_updated)
        self._chat_engine.set_callback("error", self._on_engine_error)
        self._chat_engine.set_callback(
            "user_message_added", self._on_user_message_added
        )
        self._chat_engine.set_callback("skill_requested", self._on_skill_requested)
        self._chat_engine.set_callback("question_asked", self._on_question_asked)
        self._chat_engine.set_callback("agent_switched", self._on_agent_switched)
        self._chat_engine.set_callback(
            "permission_approval_requested", self._on_permission_approval_requested
        )

        self._initialize_history_manager()
        
        # 初始化 TaskWatcher 任务观察者系统
        self._init_task_watcher()
        """初始化子智能体日志存储"""
        from app.core.sub_agent_log_store import SubAgentLogStore
        import os

        try:
            db_path = os.path.join(".drifox", "sessions.db")

            log_store = SubAgentLogStore()
            log_store.init(db_path)
            self._sub_agent_manager.set_log_store(log_store)
            logger.info(f"[LLMChatter] 子智能体日志存储初始化完成")
        except Exception as e:
            logger.error(f"[LLMChatter] 子智能体日志存储初始化失败: {e}")

        # # 自动启动 LLM API 服务
        # self._init_llm_api_service()

    def _init_task_watcher(self):
        """初始化 TaskWatcher 任务观察者系统"""
        from app.core.task_watcher import TaskWatcherSystem, get_engine_scheduler
        
        try:
            # 获取调度器
            scheduler = get_engine_scheduler()
            
            # 注册当前窗口的引擎
            canvas_name = getattr(self.homepage, "workflow_name", "default") or "default"
            engine_id = f"widget_{id(self)}"  # 使用对象 ID 作为唯一标识
            
            scheduler.register_engine(
                engine_id=engine_id,
                chat_engine=self._chat_engine,
                session_manager=self.session_manager,
                project=self._current_project
            )
            
            # 初始化或获取 TaskWatcherSystem
            if not hasattr(self, '_task_watcher') or self._task_watcher is None:
                self._task_watcher = TaskWatcherSystem(scheduler=scheduler)
                
                # 设置完成回调
                self._task_watcher.set_callback("task_completed", self._on_task_watcher_completed)
                self._task_watcher.set_callback("task_failed", self._on_task_watcher_failed)
                
                # 启动系统
                self._task_watcher.start()
                logger.info(f"[LLMChatter] TaskWatcher 系统启动完成")
            
            logger.info(f"[LLMChatter] 注册引擎到调度器: {engine_id}, project={self._current_project}")
        except Exception as e:
            logger.error(f"[LLMChatter] TaskWatcher 初始化失败: {e}")
    
    def _on_task_watcher_completed(self, config, result):
        """任务完成回调"""
        from loguru import logger
        logger.info(f"[TaskWatcher] 任务完成: {config.name}, success={result.success}")
    
    def _on_task_watcher_failed(self, config, error):
        """任务失败回调"""
        from loguru import logger
        logger.error(f"[TaskWatcher] 任务失败: {config.name}, error={error}")

    def _init_llm_api_service(self):
        """初始化 LLM API 服务"""
        from app.utils.config import Settings
        from app.api import (
            LLMAPIService,
            APISessionHandler,
            is_service_running,
        )

        setting = Settings.get_instance()

        # 注册服务商列表获取回调
        def get_providers_list():
            return [{"name": name} for name in self._valid_configs.keys()]

        # 创建并注册 API 会话处理器（复用 UI 的 ChatEngine 和 SessionManager）
        self._api_session_handler = APISessionHandler(self)
        LLMAPIService.set_session_handler(self._api_session_handler)

        # 根据配置决定是否启动服务
        if setting.llm_api_enabled.value:
            if not is_service_running():
                service = LLMAPIService()
                service.port = setting.llm_api_port.value
                service.start(background=True)
        else:
            # 确保服务未启动
            if is_service_running():
                from app.api import (
                    stop_llm_api_service,
                )

                stop_llm_api_service()

    def _setup_title_bar(self):
        """设置标题栏按钮"""
        title_bar = self.get_title_bar()
        # 显示内存标签
        title_bar.show_memory_label()
        # 创建复制窗口按钮
        self._copy_btn = TransparentToolButton(get_icon("新建窗口"), self)
        self._copy_btn.setToolTip("新建窗口")
        self._copy_btn.clicked.connect(lambda: self._duplicate_window(branch=False))
        title_bar.insert_button(0, self._copy_btn)

        # 创建分支按钮
        self._branch_btn = TransparentToolButton(get_icon("分支"), self)
        self._branch_btn.setToolTip("分支当前对话")
        self._branch_btn.clicked.connect(lambda: self._duplicate_window(branch=True))
        title_bar.insert_button(1, self._branch_btn)
        # 创建设置按钮
        self._settings_btn = TransparentToolButton(FluentIcon.SETTING, self)
        self._settings_btn.setFixedSize(28, 28)
        self._settings_btn.setToolTip("设置")
        self._settings_btn.clicked.connect(self._toggle_settings_card)
        title_bar.insert_button(2, self._settings_btn)

    def _toggle_settings_card(self):
        """切换设置卡片的显示"""
        if self._settings_popup.isVisible():
            self._settings_popup.hide()
        else:
            self._settings_popup.show()

    def _toggle_task_queue_card(self):
        """切换任务队列卡片的显示"""
        if self._task_queue_card.isVisible():
            self._task_queue_card.hide()
        else:
            # 确保 task_watcher 已初始化
            if hasattr(self, '_task_watcher') and self._task_watcher:
                self._task_queue_card.set_task_system(self._task_watcher)
            # 刷新显示
            self._task_queue_card.refresh()
            self._task_queue_card.show()

    def _open_api_docs(self):
        """打开 API 文档页面"""
        from app.api import open_docs
        open_docs()

    def _duplicate_window(self, branch: bool = False):
        """复制当前窗口并以弹窗方式显示，或从当前会话分支创建新会话
        
        Args:
            branch: 如果为 True，则复制当前会话的消息到新窗口
        """
        try:
            # 创建新的窗口实例
            new_instance = OpenAIChatToolWindow(self.homepage, None)

            # 如果是分支模式，传递当前会话的消息
            if branch:
                current_session = self.session_manager.get_current_session()
                if current_session:
                    branch_messages = list(current_session.messages)
                    branch_name = current_session.name + " [分支]"
                    # 设置分支会话数据，新窗口会使用这些消息创建会话
                    new_instance._branch_session_data = {
                        "messages": branch_messages,
                        "name": branch_name,
                    }
                # 分支模式不跳过历史恢复，而是使用传入的分支数据
                new_instance._skip_restore_history = True  # 跳过 _restore_latest_session
            else:
                new_instance._skip_restore_history = True  # 跳过历史会话恢复，创建新会话

            # 复制模型选择（确保两个实例都已初始化 UI）
            try:
                if (
                    hasattr(self, "_current_provider_name")
                    and hasattr(new_instance, "_current_provider_name")
                    and self._current_provider_name
                ):
                    new_instance._current_provider_name = self._current_provider_name
                    new_instance._current_model_name = self._current_model_name
                    new_instance._update_model_selector_btn()
            except Exception:
                pass  # 忽略模型复制失败

            # 设置 session 初始化的标志，避免重复创建新 session
            # 并标记为新会话模式，跳过历史会话恢复
            # 注意：不要设置 _session_initialized，让 showEvent 正常执行初始化
            new_instance._skip_restore_history = True  # 跳过历史会话恢复

            # 以弹窗方式显示
            from app.side_dock_area import ToolPopupDialog

            popup = ToolPopupDialog(new_instance, None)
            if branch:
                popup.setWindowTitle(f"{self.name} - 分支")
            else:
                popup.setWindowTitle(f"{self.name} - 副本")
            popup.resize(600, 900)
            # 保存引用防止被垃圾回收
            if not hasattr(self, '_popup_refs'):
                self._popup_refs = []
            self._popup_refs.append(popup)
            popup.show()
        except Exception as e:
            from qfluentwidgets import InfoBar

            InfoBar.error("复制失败", str(e), parent=self)

    def _request_tool_start_ui_sync(
        self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None
    ):
        self.toolStartUiSyncRequested.emit(
            tool_call_id, tool_name, arguments or {}, round_id or ""
        )

    def _handle_tool_start_ui_sync(
        self, tool_call_id: str, tool_name: str, arguments: object, round_id: str
    ):
        self._on_tool_call_started(tool_call_id, tool_name, arguments or {}, round_id)
        QApplication.sendPostedEvents()
        if self._tool_floating_widget:
            self._tool_floating_widget.repaint()
        self.repaint()
        QApplication.processEvents()

    def _get_chat_cards_for_engine(self):
        cards = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard):
                    cards.append(widget)
        return cards

    def _get_current_model_config(self) -> Dict[str, Any]:
        """获取当前选中的模型配置，实时从系统配置读取"""
        selected_name = self._current_provider_name if self._current_provider_name else (list(self._valid_configs.keys())[0] if self._valid_configs else "")

        setting = Settings.get_instance()

        saved_providers = setting.llm_saved_providers.value or {}
        if selected_name in saved_providers:
            return saved_providers[selected_name].copy()

        custom_vars = getattr(self.homepage, "global_variables", None)
        if custom_vars and hasattr(custom_vars, "custom"):
            if selected_name in custom_vars.custom:
                return custom_vars.custom[selected_name].value.copy()

        return self._valid_configs.get(selected_name, {})

    def _build_memory_context_for_engine(self, query: str = "") -> str:
        if not self._memory_manager:
            return ""
        return self._memory_manager.get_context_string(query=query, limit=8)

    def _get_current_session_messages_for_tools(self) -> List[Dict[str, Any]]:
        session = self.session_manager.get_current_session()
        if not session:
            return []
        return list(session.messages or [])

    def showEvent(self, event):
        if getattr(self, "_session_initialized", False):
            super().showEvent(event)
            self._connect_opacity_signal()
            return
        self._session_initialized = True

        workflow_name = getattr(self.homepage, "workflow_name", None)
        QTimer.singleShot(0, self._load_agent_list)
        
        # 如果有分支数据，延迟调用分支会话处理，避免与 _restore_latest_or_create_session 冲突
        if getattr(self, "_branch_session_data", None):
            QTimer.singleShot(50, self._apply_branch_or_create_session)
        else:
            QTimer.singleShot(0, self._restore_latest_or_create_session)
        
        QTimer.singleShot(100, self._load_model_configs)
        self._connect_opacity_signal()
        super().showEvent(event)

    def eventFilter(self, obj, event):
        """处理 viewport 大小变化，调整背景图片"""
        if obj == self.chat_scroll_area.viewport() and event.type() == event.Type.Resize:
            if hasattr(self, "_bg_label"):
                self._bg_label.resize(self.chat_scroll_area.viewport().size())
        return super().eventFilter(obj, event)

    def _connect_opacity_signal(self):
        """连接父窗口的透明度变化信号"""
        if getattr(self, "_opacity_signal_connected", False):
            return
        parent = self.parent()
        if parent and hasattr(parent, "globalOpacityChanged"):
            parent.globalOpacityChanged.connect(self._on_global_opacity_changed)
            self._opacity_signal_connected = True

    def _on_global_opacity_changed(self, opacity: float):
        """响应全局透明度变化，更新所有子组件的透明度"""
        self._update_widgets_opacity(opacity)

    def _update_widgets_opacity(self, opacity: float):
        """更新所有需要响应透明度变化的组件"""
        # 更新待办事项悬浮框
        if self._todo_floating_widget:
            self._todo_floating_widget.set_opacity(opacity)
        # 更新模型配置卡片
        if self._model_config_card:
            self._model_config_card.set_opacity(opacity)
        # 更新历史会话卡片
        if self._history_card:
            self._history_card.set_opacity(opacity)
        # 更新设置卡片
        if self._settings_popup:
            self._settings_popup.set_opacity(opacity)
        # 更新子智能体悬浮框
        if hasattr(self, "_sub_agent_floating_widget") and self._sub_agent_floating_widget:
            self._sub_agent_floating_widget.set_opacity(opacity)
        # 更新工具悬浮框
        if hasattr(self, "_tool_floating_widget") and self._tool_floating_widget:
            self._tool_floating_widget.set_opacity(opacity)
        # 更新问题悬浮框
        if self._question_floating_widget:
            self._question_floating_widget.set_opacity(opacity)

    def _restore_latest_or_create_session(self):
        # 如果是新复制的窗口，跳过历史会话恢复
        if getattr(self, "_skip_restore_history", False):
            self._create_new_session()
            return
        if self._restore_latest_session():
            return
        self._create_new_session()

    def _apply_branch_or_create_session(self):
        """处理分支会话或创建新会话"""
        branch_data = getattr(self, "_branch_session_data", None)
        if branch_data:
            # 使用分支数据创建会话
            self._create_branched_session(
                branch_data.get("messages", []),
                branch_data.get("name", "分支对话"),
            )
        else:
            # 没有分支数据，创建新会话
            self._create_new_session()

    def _create_branched_session(self, messages: List[Dict], name: str):
        """创建分支会话并渲染消息"""
        logger.info("[Branch] 开始创建分支会话")
        
        if self._is_streaming and self._chat_engine:
            self._chat_engine.stop()
            self._is_streaming = False
            self._toggle_send_stop(False)

        self._cache_current_session_cards()
        session = self.session_manager.create_new_session()
        session.messages = messages
        session.name = name
        self._current_session_id = session.session_id

        # 清空聊天区域
        self._clear_chat_area()
        self.title_edit.setText(name)
        self.node_preview.clear_nodes()

        # 重置输入框高度
        if hasattr(self, 'input_area'):
            logger.info(f"[Branch] 重置输入框高度，当前: {self.input_area.height()}")
            self.input_area._initializing = True
            self.input_area.setFixedHeight(72)
            self.input_area._initializing = False

        # 复用现有的会话显示逻辑
        self._display_current_session()

        # 滚动到底部
        QTimer.singleShot(50, self._scroll_to_bottom)
        QTimer.singleShot(150, self._scroll_to_bottom)

    def _create_new_session(self):
        if self._is_streaming and self._chat_engine:
            self._chat_engine.stop()
            self._is_streaming = False
            self._toggle_send_stop(False)

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception(
                "Failed to auto-save current session before creating a new one"
            )

        self._cache_current_session_cards()
        session = self.session_manager.create_new_session()
        self._current_session_id = session.session_id
        self._history_preview_messages = None
        self._clear_chat_area()
        self.title_edit.setText("新对话")
        self.node_preview.clear_nodes()
        if self._todo_floating_widget:
            self._todo_floating_widget.clear()
        if self._tool_executor:
            self._tool_executor.clear_todo_list()
            self._tool_executor.set_session_context(self._current_session_id)
        if self._question_floating_widget:
            self._question_floating_widget.clear()
        self._question_tool_call_id = None
        self._load_agent_list()
        QTimer.singleShot(0, self._show_initial_welcome)
        self._refresh_context_usage_indicator()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(1)

        self.setStyleSheet(WINDOW_STYLE)

        session_bar_layout = QHBoxLayout()
        session_bar_layout.setContentsMargins(0, 0, 0, 0)
        session_bar_layout.setSpacing(4)

        left_layout = QHBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # 项目选择标签
        self._current_project = "默认项目"
        self._project_label = QLabel(self._current_project, self)
        self._project_label.setStyleSheet(f"""
            QLabel {{
                color: #f59e0b;
                {get_font_family_css()}
                font-size: 13px;
                font-weight: bold;
                padding: 2px 6px;
                border-radius: 4px;
                background: rgba(245, 158, 11, 0.1);
            }}
            QLabel:hover {{
                background: rgba(245, 158, 11, 0.2);
            }}
        """)
        self._project_label.setCursor(Qt.PointingHandCursor)
        self._project_label.mousePressEvent = self._on_project_label_clicked
        self._project_label.setToolTip("点击切换项目")

        # 分隔符
        self._title_sep = StrongBodyLabel(" / ", self)

        # 标题
        self.title_edit = QLabel("新对话", self)
        font_css = get_font_family_css()
        title_style = TITLE_STYLE.replace("    QLabel {", f"    QLabel {{\n        {font_css}")
        self.title_edit.setStyleSheet(title_style)
        self.title_edit.setCursor(Qt.PointingHandCursor)
        self.title_edit.mouseDoubleClickEvent = self._on_title_double_click

        left_layout.addWidget(self._project_label)
        left_layout.addWidget(self._title_sep)
        left_layout.addWidget(self.title_edit)

        self.menu_btn = TransparentToolButton(FluentIcon.MORE, self)
        self.menu_btn.setFixedSize(26, 26)
        self.menu_btn.setToolTip("更多操作")
        self._create_context_menu()
        left_layout.addWidget(self.menu_btn)

        # right_layout 保持简化，显示余额和 context_usage_ring
        right_layout = QHBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # 余额显示
        from app.widgets.balance_display import BalanceDisplay
        self.balance_display = BalanceDisplay(self)
        right_layout.addWidget(self.balance_display)

        # 上下文占用圆环
        self.context_usage_ring = ContextUsageRing(self)
        right_layout.addWidget(self.context_usage_ring)
        right_layout.addSpacing(10)

        session_bar_layout.addLayout(left_layout)
        session_bar_layout.addStretch()
        session_bar_layout.addLayout(right_layout)
        layout.addLayout(session_bar_layout)

        self._settings_popup = LLMSettingsCard(self)
        self._settings_popup.setVisible(False)
        self._settings_popup.closed.connect(self._on_settings_closed)
        self._settings_popup.configChanged.connect(self._load_model_configs)

        self._todo_floating_widget = TodoFloatingWidget(self)
        self._todo_floating_widget.setVisible(False)
        layout.addWidget(self._todo_floating_widget)

        self._sub_agent_floating_widget = SubAgentFloatingWidget(self)
        self._sub_agent_floating_widget.setVisible(False)

        self._tool_floating_widget = ToolFloatingWidget(self)
        self._tool_floating_widget.setVisible(False)

        layout.addWidget(self._settings_popup)

        self.chat_scroll_area = SingleDirectionScrollArea(self)
        self.chat_scroll_area.setMinimumHeight(10)
        self.chat_scroll_area.setMinimumWidth(400)
        self.chat_scroll_area.setStyleSheet(CHAT_SCROLL_STYLE)
        self.chat_scroll_area.setWidgetResizable(True)
        self.chat_scroll_area.setViewportMargins(2, 2, 10, 2)
        self.chat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 背景图片层 - 半透明、随窗口缩放
        viewport = self.chat_scroll_area.viewport()
        self._bg_label = QLabel(viewport)
        self._bg_label.setPixmap(QPixmap(":/icons/fox_bg.png"))
        self._bg_label.setScaledContents(True)  # 允许缩放
        self._bg_opacity = QGraphicsOpacityEffect(self._bg_label)
        self._bg_opacity.setOpacity(0.1)  # 25% 透明度
        self._bg_label.setGraphicsEffect(self._bg_opacity)
        self._bg_label.lower()  # 放到底层
        self._bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # 鼠标事件穿透
        self._bg_label.resize(viewport.size())
        self._bg_label.show()
        viewport.installEventFilter(self)

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(8, 8, 8, 8)
        self.chat_layout.setSpacing(8)
        self.chat_layout.setAlignment(Qt.AlignBottom)
        self.chat_scroll_area.setWidget(self.chat_container)

        layout.addWidget(self.chat_scroll_area, 1)

        layout.addWidget(self._sub_agent_floating_widget)
        layout.addWidget(self._tool_floating_widget)

        # 模型配置卡片 - 在消息列表下方，和工具卡片同位置
        self._model_config_card = BaseSettingsCard("模型配置", "🔧", self)
        self._model_config_card.setMaximumHeight(120)
        self._model_config_popup = ModelConfigCard()
        self._model_config_popup.configApplied.connect(self._on_config_applied)
        self._model_config_card.content_layout.addWidget(self._model_config_popup)
        self._model_config_card.setVisible(False)
        layout.addWidget(self._model_config_card)

        # 历史会话卡片 - 在消息列表下方，和工具卡片同位置
        self._history_card = BaseSettingsCard("历史会话", "📜", self)
        self._history_card.setFixedHeight(350)
        # 设置历史/归档标签
        self._history_card.setup_tabs([
            ("history", "历史会话"),
            ("archived", "归档"),
        ], "history")
        self._history_card.tabChanged.connect(self._on_history_tab_changed)

        self._history_popup_card = HistoryCard()
        self._history_popup_card.sessionSelected.connect(self._on_history_session_selected)
        self._history_popup_card.sessionArchived.connect(self._archive_history_session)
        self._history_popup_card.sessionRenamed.connect(self._rename_history_session)
        self._history_popup_card.refreshRequested.connect(self._refresh_history_toggle_panel)
        self._history_popup_card.sessionImported.connect(self._on_session_imported)
        # 归档会话相关信号
        self._history_popup_card.sessionRestored.connect(self._on_archived_session_restored)
        self._history_popup_card.sessionPermanentlyDeleted.connect(self._on_archived_session_deleted)
        self._history_popup_card.archivedSessionRenamed.connect(self._on_archived_session_renamed)
        # 设置导入按钮的处理器
        self._history_card.set_extra_button_handler(
            self._history_popup_card.get_import_button_handler()
        )

        # 历史会话卡片
        self._history_card.content_layout.addWidget(self._history_popup_card)
        self._history_card.setVisible(False)
        layout.addWidget(self._history_card)
        
        # 任务队列卡片
        from app.widgets.task_queue_card import TaskQueueCard
        self._task_queue_card = TaskQueueCard(self)
        self._task_queue_card.setFixedHeight(300)
        self._task_queue_card.setVisible(False)
        layout.addWidget(self._task_queue_card)

        self.node_preview = ConversationNodePreview(self)
        self.node_preview.nodeClicked.connect(self._on_node_preview_clicked)
        layout.addWidget(self.node_preview)

        self.chat_scroll_area.verticalScrollBar().valueChanged.connect(
            self._on_scroll_changed
        )

        self._question_floating_widget = QuestionFloatingWidget(self)
        self._question_floating_widget.setVisible(False)
        self._question_floating_widget.answered.connect(self._on_question_answered)
        self._question_floating_widget.cancelled.connect(self._on_question_cancelled)
        layout.addWidget(self._question_floating_widget)

        hlayout = QHBoxLayout()
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.setSpacing(4)

        # 模型选择 + 配置按钮组 - 紧凑式设计
        self._model_btn_container = QWidget(self)
        self._model_btn_container.setFixedHeight(30)
        self._model_btn_container.setStyleSheet("""
            background: rgba(27, 35, 50, 180);
            border: 1px solid rgba(43, 56, 80, 200);
            border-radius: 12px;
        """)
        model_layout = QHBoxLayout(self._model_btn_container)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(0)
        
        # 模型选择按钮（可点击弹出模型选择）
        self.current_model_btn = QWidget(self._model_btn_container)
        self.current_model_btn.setCursor(Qt.PointingHandCursor)
        self.current_model_btn.setStyleSheet(MODEL_BTN_STYLE)
        self.current_model_btn.setMouseTracking(True)
        self.current_model_btn.mousePressEvent = lambda e: self._show_model_selector_popup()
        btn_layout = QHBoxLayout(self.current_model_btn)
        btn_layout.setContentsMargins(8, 4, 0, 4)
        btn_layout.setSpacing(4)
        self._model_btn_icon = QLabel(self.current_model_btn)
        self._model_btn_icon.setStyleSheet("""background: transparent; border: none;""")
        self._model_btn_icon.setFixedSize(18, 18)
        btn_layout.addWidget(self._model_btn_icon)
        self._model_btn_text = QLabel("正在加载...", self.current_model_btn)
        self._model_btn_text.setStyleSheet(MODEL_BTN_TEXT_STYLE)
        btn_layout.addWidget(self._model_btn_text)
        model_layout.addWidget(self.current_model_btn, 1)
        # 配置按钮（点击弹出配置卡片）
        self.settings_btn = TransparentToolButton(get_icon("模型选择"), self._model_btn_container)
        self.settings_btn.setFixedSize(26, 26)
        self.settings_btn.setToolTip("模型参数配置")
        self.settings_btn.clicked.connect(self._toggle_model_config_card)
        model_layout.addWidget(self.settings_btn)
        
        hlayout.addWidget(self._model_btn_container)
        
        # 记下当前选中的服务商和模型，供弹窗使用
        self._current_provider_name = ""
        self._current_model_name = ""

        # 智能体切换按钮组 - 金属质感+简约科技风
        self._agent_switch_widget = self._create_agent_switch_buttons()
        hlayout.addWidget(self._agent_switch_widget)

        hlayout.addStretch(1)

        # 工具栏右侧按钮组 - 胶囊包裹，无分隔线
        self._toolbar_capsule = QWidget(self)
        self._toolbar_capsule.setFixedHeight(30)
        self._toolbar_capsule.setStyleSheet("""
            background: rgba(27, 35, 50, 180);
            border: 1px solid rgba(43, 56, 80, 200);
            border-radius: 12px;
        """)
        capsule_layout = QHBoxLayout(self._toolbar_capsule)
        capsule_layout.setContentsMargins(4, 2, 4, 2)
        capsule_layout.setSpacing(0)

        # Diff 按钮 - 查看文件差异
        self.diff_btn = TransparentToolButton(get_icon("差异对比"), self._toolbar_capsule)
        self.diff_btn.setFixedSize(26, 26)
        self.diff_btn.setToolTip("查看文件差异")
        self.diff_btn.clicked.connect(self._open_diff_viewer)
        capsule_layout.addWidget(self.diff_btn)

        # 记忆按钮
        self.memory_btn = TransparentToolButton(get_icon("长期记忆"), self._toolbar_capsule)
        self.memory_btn.setFixedSize(26, 26)
        self.memory_btn.setToolTip("长期记忆管理")
        self.memory_btn.clicked.connect(self._show_soul_memory)
        capsule_layout.addWidget(self.memory_btn)
        
        # 任务队列按钮
        self.task_queue_btn = TransparentToolButton(get_icon("任务队列"), self._toolbar_capsule)
        self.task_queue_btn.setFixedSize(26, 26)
        self.task_queue_btn.setToolTip("任务队列管理")
        self.task_queue_btn.clicked.connect(self._toggle_task_queue_card)
        capsule_layout.addWidget(self.task_queue_btn)

        # 历史按钮
        self.history_btn = TransparentToolButton(FluentIcon.HISTORY, self._toolbar_capsule)
        self.history_btn.setFixedSize(26, 26)
        self.history_btn.setToolTip("历史会话")
        self.history_btn.clicked.connect(self._toggle_history_card)
        capsule_layout.addWidget(self.history_btn)

        # 新建按钮
        self.new_session_btn = TransparentToolButton(FluentIcon.ADD, self._toolbar_capsule)
        self.new_session_btn.setFixedSize(26, 26)
        self.new_session_btn.setToolTip("新建对话")
        self.new_session_btn.clicked.connect(self._create_new_session)
        capsule_layout.addWidget(self.new_session_btn)

        hlayout.addWidget(self._toolbar_capsule)

        layout.addLayout(hlayout)

        # 输入框 - 在工具栏下方
        self.input_area = SendableTextEdit(self)
        self.input_area._agent_combo.hide()  # 隐藏输入框内部的下拉框，用工具栏的按钮组代替
        self.input_area._initializing = False  # 初始化完成后启用高度调整
        setFont(self.input_area, 15)
        self.input_area.sendMessageRequested.connect(self._on_send_clicked)
        self.input_area.stopMessageRequested.connect(self._on_stop_clicked)
        self.input_area.clearRequested.connect(self._on_clear_shortcut)
        self.input_area.newSessionRequested.connect(self._create_new_session)
        self.input_area.agentChanged.connect(self._on_agent_changed)
        layout.addWidget(self.input_area)

    def _show_model_selector_popup(self):
        """显示扁平式模型选择上拉框"""
        provider_models_data = []
        for provider_name, config in self._valid_configs.items():
            model_list = []
            if "模型列表" in config:
                saved_models = config["模型列表"]
                if isinstance(saved_models, str):
                    try:
                        import ast; saved_models = ast.literal_eval(saved_models)
                    except Exception:
                        saved_models = []
                if isinstance(saved_models, list):
                    model_list = list(saved_models)
            elif provider_name in PROVIDER_MODELS:
                model_list = list(PROVIDER_MODELS[provider_name])
            cur_model = config.get("模型名称", "")
            if cur_model and cur_model not in model_list:
                model_list.insert(0, cur_model)
            if not model_list and cur_model:
                model_list = [cur_model]
            is_current = provider_name == self._current_provider_name
            provider_models_data.append((provider_name, model_list, is_current))

        if not hasattr(self, "_model_selector_popup") or not self._model_selector_popup:
            from app.widgets.model_selector_popup import (
                ModelSelectorPopup, ProviderConfigListDialog,
            )
            from app.widgets.provider_setting_card import ProviderEditDialog
            self._model_selector_popup = ModelSelectorPopup(self)
            self._model_selector_popup.modelSelected.connect(self._on_model_selected_from_popup)
            self._model_selector_popup.addProviderClicked.connect(
                lambda: self._on_add_provider_from_popup()
            )
            self._model_selector_popup.configureProviderClicked.connect(
                lambda: self._on_configure_providers_from_popup()
            )

        self._model_selector_popup.set_providers_data(
            provider_models_data, self._current_provider_name or "", self._current_model_name or "",
        )
        self._model_selector_popup.show_at(self.current_model_btn)

    def _on_add_provider_from_popup(self):
        """从模型选择弹窗点击「添加」按钮 - 弹出添加服务商窗口"""
        self._model_selector_popup.close()
        from app.widgets.provider_setting_card import ProviderEditDialog
        dialog = ProviderEditDialog("", {}, True, self)
        if dialog.exec():
            name, info = dialog.get_result()
            # 刷新配置并重新加载弹窗
            self._load_model_configs()
            # 如果添加的服务商有模型，自动选中它
            if name and info.get("模型名称"):
                self._on_model_selected_from_popup(name, info.get("模型名称", ""))

    def _on_configure_providers_from_popup(self):
        """从模型选择弹窗点击「配置」按钮 - 显示设置卡片并展开服务商配置"""
        self._model_selector_popup.close()
        # 打开设置卡片
        self._settings_popup.show()
        self._settings_popup.raise_()
        # 展开「已保存的服务商」下拉
        self._settings_popup.llmProviderCard.setExpand(True)
        # 滚动到顶部
        QTimer.singleShot(100, self._scroll_settings_to_top)

    def _scroll_settings_to_top(self):
        """滚动设置卡片内容到顶部"""
        try:
            # 找到 LLMSettingsCard 内部的 QScrollArea 并滚到顶
            scroll_areas = self._settings_popup.findChildren(QScrollArea)
            if scroll_areas:
                scroll_areas[0].verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _on_model_selected_from_popup(self, provider_name: str, model_name: str):
        """从弹窗选中模型后切换"""
        self._current_provider_name = provider_name
        self._current_model_name = model_name
        if provider_name in self._valid_configs:
            self._valid_configs[provider_name]["模型名称"] = model_name
        setting = Settings.get_instance()
        setting.set(setting.llm_selected_model, provider_name, save=True)
        
        # 关键修复：同步更新 saved_providers 中的模型名称，
        # 确保 ChatEngine 的 _get_current_model_config 能读到正确的模型名
        saved_providers = setting.llm_saved_providers.value or {}
        if provider_name in saved_providers:
            saved_providers[provider_name]["模型名称"] = model_name
            setting.set(setting.llm_saved_providers, saved_providers, save=True)
        
        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()
        self._update_balance_display()

    def _update_model_selector_btn(self):
        """更新模型选择按钮的图标和文字显示"""
        if not hasattr(self, "current_model_btn"):
            return
        # 设置图标
        icon = None
        if self._current_provider_name:
            icon_name = PROVIDER_ICONS.get(self._current_provider_name, "")
            if icon_name:
                icon = get_icon(icon_name)

        if icon and not icon.isNull():
            self._model_btn_icon.setPixmap(icon.pixmap(18, 18))
        else:
            self._model_btn_icon.clear()

        # 设置文字
        if self._current_provider_name and self._current_model_name:
            self._model_btn_text.setText(self._current_model_name)
            self.current_model_btn.setToolTip(f"{self._current_provider_name} · {self._current_model_name}")
        elif self._current_provider_name:
            self._model_btn_text.setText(self._current_provider_name)
            self.current_model_btn.setToolTip(self._current_provider_name)
        else:
            self._model_btn_text.setText("选择模型...")
            self.current_model_btn.setToolTip("")

        self._update_balance_display()

    def _on_context_selection_changed(self, _selected_keys=None):
        self._refresh_context_usage_indicator()

    def _refresh_context_usage_indicator(self):
        ring = getattr(self, "context_usage_ring", None)
        if not ring:
            return

        if not self._chat_engine:
            ring.set_usage(0, 0, 0)
            return

        session = self.session_manager.get_current_session()
        llm_config = self._get_current_model_config()
        snapshot = self._chat_engine.get_context_usage_snapshot(session, llm_config)
        ring.set_usage(
            snapshot.get("percent", 0),
            snapshot.get("used_tokens", 0),
            snapshot.get("budget_tokens", 0),
            snapshot.get("compaction", {}),
        )

    def _update_balance_display(self):
        """更新余额显示"""
        balance_display = getattr(self, "balance_display", None)
        if not balance_display:
            return

        # 获取当前选中的服务商配置
        provider_name = getattr(self, "_current_provider_name", "")
        if not provider_name:
            balance_display.clear()
            return

        config = self._valid_configs.get(provider_name, {})
        api_key = config.get("API_KEY", "")

        balance_display.set_provider(provider_name, api_key)

    def _open_settings_popup(self):
        """打开设置卡片"""
        self._hide_main_popups()
        self._settings_popup.show()
        self._settings_popup.raise_()
        self._settings_popup.activateWindow()

    def _on_settings_closed(self):
        """设置卡片关闭时的回调"""
        # 可以在这里添加一些清理逻辑
        pass

    def _hide_main_popups(self):
        """隐藏主要的悬浮面板（互斥显示）
        
        包括：系统设置、模型配置、历史会话
        不包括：工具悬浮、Todo、子智能体等工具类浮窗
        """
        self._model_config_card.hide()
        self._history_card.hide()
        self._settings_popup.hide()

    def _toggle_model_config_card(self):
        """切换模型配置卡片的显示"""
        # 切换当前卡片
        if self._model_config_card.isVisible():
            self._model_config_card.hide()
        else:
            self._hide_main_popups()  # 隐藏其他主面板
            # 每次打开都重新加载配置
            self._load_model_config_to_card()
            self._model_config_card.show()

    def _load_model_config_to_card(self):
        """加载当前模型配置到卡片（仅参数配置，不显示连接信息）"""
        current_name = self._current_provider_name if self._current_provider_name else "无"
        setting = Settings.get_instance()

        saved_providers = setting.llm_saved_providers.value or {}
        provider_config = saved_providers.get(current_name, {})
        custom_vars = getattr(self.homepage, "global_variables", None)
        if current_name in (
            custom_vars.custom
            if custom_vars and hasattr(custom_vars, "custom")
            else {}
        ):
            config = custom_vars.custom[current_name].value.copy()
        else:
            config = provider_config.copy()
            config.pop("备注", None)
            config.pop("获取地址", None)
        # 只保留参数配置，移除连接信息
        config.pop("模型名称", None)
        config.pop("API_URL", None)
        config.pop("API_KEY", None)
        config.pop("模型列表", None)

        self._model_config_popup.set_config(current_name, config)

    def _create_agent_switch_buttons(self) -> QWidget:
        """创建智能体切换按钮 - 单胶囊设计，中间用分隔线"""
        container = QWidget()
        container.setFixedHeight(30)
        container.setStyleSheet("""
            background: rgba(27, 35, 50, 180);
            border: 1px solid rgba(43, 56, 80, 200);
            border-radius: 12px;
        """)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)
        
        # 加载智能体列表
        agents = self._agent_manager.list_primary_agents() if self._agent_manager else []
        
        # 如果没有智能体，显示占位文本
        if not agents:
            placeholder = QLabel("无可用智能体")
            placeholder.setStyleSheet(f"""
                QLabel {{
                    color: #8FA4C2;
                    font-size: 12px;
                    padding: 0 12px;
                    {get_font_family_css()}
                }}
            """)
            layout.addWidget(placeholder)
            return container
        
        # 默认选中的智能体
        default_agent = getattr(self, '_current_agent', 'plan')
        
        self._agent_buttons = {}
        self._agent_btn_group = QButtonGroup()
        self._agent_btn_group.buttonClicked[int].connect(self._on_agent_btn_clicked)
        
        # 默认样式
        default_style = f"""
            QPushButton {{
                background: transparent;
                color: #8FA4C2;
                border: none;
                border-radius: 8px;
                padding: 4px 12px;
                font-size: 12px;
                font-weight: 500;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.05);
                color: #B4C2D9;
            }}
        """
        
        # 选中样式
        selected_style = f"""
            QPushButton {{
                background: rgba(201, 168, 92, 0.2);
                color: #C9A85C;
                border: none;
                border-radius: 8px;
                padding: 4px 12px;
                font-size: 12px;
                font-weight: 600;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background: rgba(201, 168, 92, 0.25);
            }}
        """
        
        for i, agent in enumerate(agents):
            # 添加分隔线（在按钮之前，除了第一个）
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.VLine)
                sep.setFixedWidth(1)
                sep.setStyleSheet("background: rgba(60, 75, 95, 150); margin: 4px 0;")
                layout.addWidget(sep)
            
            btn = QPushButton(agent.name)
            btn.setFixedHeight(22)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setStyleSheet(default_style)
            btn.setToolTip(agent.description)
            
            self._agent_btn_group.addButton(btn, i)
            self._agent_buttons[agent.name] = {"btn": btn, "style": default_style, "selected_style": selected_style}
            layout.addWidget(btn)
            
            # 如果是默认选中的智能体，则选中它
            if agent.name == default_agent:
                btn.setChecked(True)
                btn.setStyleSheet(selected_style)
        
        # 如果没有匹配默认智能体，选中第一个
        if default_agent not in self._agent_buttons and agents:
            btn = self._agent_buttons[agents[0].name]["btn"]
            btn.setChecked(True)
            btn.setStyleSheet(selected_style)
            self._current_agent = agents[0].name
        
        return container
    
    def _on_agent_btn_clicked(self, btn_id: int):
        """智能体按钮点击处理"""
        agents = self._agent_manager.list_primary_agents() if self._agent_manager else []
        if btn_id >= len(agents):
            return
        
        agent = agents[btn_id]
        agent_name = agent.name
        
        logger.info(f"[_on_agent_btn_clicked] btn_id={btn_id}, agent_name={agent_name}, _current_agent before={self._current_agent}")
        
        # 更新按钮样式
        for name, data in self._agent_buttons.items():
            btn = data["btn"]
            if name == agent_name:
                btn.setStyleSheet(data["selected_style"])
            else:
                btn.setStyleSheet(data["style"])
        
        # 触发智能体切换
        self._on_agent_changed(agent_name)

    def _toggle_history_card(self):
        """切换历史会话卡片的显示"""
        # 切换当前卡片
        if self._history_card.isVisible():
            self._history_card.hide()
        else:
            self._hide_main_popups()  # 隐藏其他主面板
            self._history_card.show()
            # 刷新数据
            self._refresh_history_toggle_panel()

    def _refresh_history_toggle_panel(self):
        """刷新历史面板数据"""
        current_tab = self._history_card._current_tab if hasattr(self._history_card, '_current_tab') else "history"
        
        if current_tab == "history":
            # 刷新历史会话 - 使用 _current_project（从配置加载的）
            current_idx = (
                self.history_manager.find_index_by_session_id(self._current_session_id)
                if self._current_session_id and self.history_manager
                else None
            )
            history_list = self.history_manager.get_history_list(self._current_project) if self.history_manager else []
            self._history_popup_card.set_history(history_list, current_idx)
        else:
            # 刷新归档会话
            self._refresh_archived_sessions()

    def _on_history_project_selected(self, project: str):
        """历史面板项目切换（现在和标题栏同步）"""
        self._current_project = project
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()

    def _on_session_dropped_on_project(self, project: str, session_index: int):
        """将会话拖拽到指定项目"""
        if not self.history_manager:
            return
        history_list = self.history_manager.get_history_list(self._current_project) if self.history_manager else []
        if 0 <= session_index < len(history_list):
            # 获取 session_id
            session = history_list[session_index]
            session_id = session.get("session_id")
            if session_id:
                # 更新项目的 session 记录
                idx = self.history_manager.find_index_by_session_id(session_id)
                if idx is not None:
                    self.history_manager.move_to_project(idx, project)
                    # 刷新
                    self._history_popup_card.refreshRequested.emit()
                    InfoBar.success("已移动", f"会话已移至「{project}」项目", duration=2000, parent=self)

    def _refresh_archived_sessions(self):
        """刷新归档会话列表"""
        if not self.history_manager:
            return
        
        archived_list = self.history_manager.get_archived_sessions()
        # 为每个归档会话添加预览信息
        for session in archived_list:
            try:
                with open(session["path"], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    messages = data.get("messages", [])
                    session["message_count"] = data.get("message_count", len([m for m in messages if m.get("role") == "user"]))
                    session["last_time"] = data.get("last_time", data.get("saved_at", ""))
                    session["preview"] = get_message_preview(messages) if messages else ""
            except Exception:
                pass
        
        self._history_popup_card.set_archived_sessions(archived_list)

    def _on_history_tab_changed(self, tab_id: str):
        """处理历史/归档标签切换"""
        self._history_popup_card.switch_tab(tab_id)
        if tab_id == "archived":
            self._refresh_archived_sessions()
        else:
            self._refresh_history_toggle_panel()

    def _on_history_session_selected(self, index: int):
        """从历史面板选择会话"""
        if index == -1:
            # 新建会话
            self._create_new_session()
        else:
            self._load_history_session_from_popup(index)
        # 关闭历史会话卡片
        self._history_card.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._set_cards_resize_preview_mode(True)
        # resize 期间持续重置防抖，避免在拖拽过程中提前批量重排
        self._pending_resize_sync = True
        self._resize_debounce_timer.stop()
        self._resize_debounce_timer.start()
        self._resize_complete_timer.stop()
        self._resize_complete_timer.start()

    def _set_cards_resize_preview_mode(self, enabled: bool):
        if enabled == self._resize_preview_active:
            return

        self._resize_preview_active = enabled
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            item.widget().set_resize_preview_mode(enabled)

    def _do_debounced_resize(self):
        """防抖执行卡片宽度同步 - resize 期间同步所有可见卡片宽度"""
        self._pending_resize_sync = False

        # 获取滚动区域视口
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width <= 0:
                return
            self._last_chat_viewport_width = viewport_width
            viewport_rect = scroll_area.viewport().rect()
            viewport_top = scroll_area.verticalScrollBar().value()
            viewport_bottom = viewport_top + viewport_rect.height()
        
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            
            card = item.widget()
            
            # resize 期间同步所有卡片的宽度，不做可见性过滤
            # 占位符模式下只更新宽高，不触发复杂重绘
            card.sync_width()
    
    def _sync_all_cards_width(self):
        """resize 完成后更新所有卡片的宽度（包括非可见区域的）"""
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width > 0:
                self._last_chat_viewport_width = viewport_width
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), MessageCard):
                item.widget().sync_width(force=True)
        self._set_cards_resize_preview_mode(False)
    
    def _sync_visible_cards_on_scroll(self):
        """滚动时更新新进入可见区域的卡片"""
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if not scroll_area:
            return
        
        viewport_rect = scroll_area.viewport().rect()
        viewport_top = scroll_area.verticalScrollBar().value()
        viewport_bottom = viewport_top + viewport_rect.height()
        
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            
            card = item.widget()
            card_rect = card.geometry()
            card_top = card_rect.top()
            card_bottom = card_rect.bottom()
            
            # 只更新可见区域附近（缓冲200px）的卡片
            if card_bottom < viewport_top - 200 or card_top > viewport_bottom + 200:
                continue
            
            card.sync_width()

    def _on_config_applied(self, new_config: dict):
        current_name = self._current_provider_name
        if not current_name:
            return
        is_free_provider = current_name in FREE_PROVIDERS

        if is_free_provider:
            # 只更新参数，保留连接信息
            saved_providers = Settings.get_instance().llm_saved_providers.value or {}
            old_config = saved_providers.get(current_name, self._valid_configs.get(current_name, {}))
            old_config.update(new_config)
            self._valid_configs[current_name] = old_config
            saved_providers[current_name] = old_config
            Settings.get_instance().set(Settings.get_instance().llm_saved_providers, saved_providers, save=True)
            self._load_model_configs()
            InfoBar.success("已保存", "配置已保存到本地。", parent=self, duration=1500)
        else:
            if (
                hasattr(self.homepage, "global_variables")
                and self.homepage.global_variables
            ):
                custom_vars = self.homepage.global_variables.custom
                if current_name in custom_vars:
                    old_config = custom_vars[current_name].value
                    old_config.update(new_config)
                    custom_vars[current_name].value = old_config
                    self.homepage._on_global_variables_changed(
                        "custom", current_name, "update"
                    )

                self._load_model_configs()
                InfoBar.success(
                    "已保存", "配置已保存到自定义配置。", parent=self, duration=1500
                )
            else:
                InfoBar.warning(
                    "无法保存",
                    "当前页面不支持保存自定义配置。",
                    parent=self,
                    duration=1500,
                )

    def _load_model_configs(self):
        setting = Settings.get_instance()
        saved_model = setting.llm_selected_model.value
        old_provider = self._current_provider_name
        old_model = self._current_model_name

        self._valid_configs.clear()

        setting = Settings.get_instance()
        default_config = {
            "模型名称": setting.llm_model.value,
            "API_KEY": setting.llm_api_key.value,
            "API_URL": setting.llm_api_base.value,
            "最大Token": setting.llm_max_tokens.value,
            "温度": setting.llm_temperature.value,
            "启用技能": setting.llm_enabled_skills.value,
        }
        try:
            custom_vars = getattr(self.homepage, "global_variables", None)
            if custom_vars and hasattr(custom_vars, "custom"):
                for config_name, var_obj in custom_vars.custom.items():
                    if hasattr(var_obj, "value") and isinstance(var_obj.value, dict):
                        val = var_obj.value
                        if {"API_URL", "API_KEY", "模型名称"}.issubset(val.keys()):
                            self._valid_configs[config_name] = val
        except Exception as e:
            logger.error(f"[ERROR] 加载自定义模型配置失败: {e}")

        saved_providers = setting.llm_saved_providers.value or {}
        for provider_name in saved_providers:
            config = saved_providers[provider_name].copy()
            config.pop("备注", None)
            config.pop("获取地址", None)
            self._valid_configs[provider_name] = config

        # 恢复或设置当前选中的服务商和模型
        if saved_model and saved_model in self._valid_configs:
            self._current_provider_name = saved_model
        elif old_provider and old_provider in self._valid_configs:
            self._current_provider_name = old_provider
        else:
            self._current_provider_name = list(self._valid_configs.keys())[0] if self._valid_configs else ""

        if self._current_provider_name:
            provider_config = self._valid_configs.get(self._current_provider_name, {})
            self._current_model_name = provider_config.get("模型名称", "")
        else:
            self._current_model_name = ""

        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()

    def _load_agent_list(self):
        """加载智能体列表到按钮组（仅显示 primary agents）"""
        if not self._agent_manager:
            return
        if not hasattr(self, "_agent_btn_group"):
            return  # 按钮组还未创建
        
        self._suppress_agent_intro = True
        agents = self._agent_manager.list_primary_agents()
        buttons = self._agent_btn_group.buttons()
        default_agent = getattr(self, '_current_agent', 'build')
        
        # 更新按钮文本和提示
        for i, agent in enumerate(agents):
            if i < len(buttons):
                btn = buttons[i]
                btn.setText(agent.name)
                btn.setToolTip(agent.description)
        
        # 根据当前智能体选中对应按钮
        found = False
        for i, agent in enumerate(agents):
            if i < len(buttons) and agent.name == default_agent:
                buttons[i].setChecked(True)
                self._update_agent_button_style(default_agent)
                found = True
                logger.info(f"[_load_agent_list] Found match for {default_agent}, btn_id={i}")
                break
        
        if not found:
            # 如果没找到匹配的，默认选中第一个
            logger.warning(f"[_load_agent_list] {default_agent} not found, using agents[0]={agents[0].name if agents else 'None'}")
            if buttons:
                buttons[0].setChecked(True)
                self._current_agent = agents[0].name if agents else "build"
                self._update_agent_button_style(self._current_agent)
        
        # 同步 ChatEngine 的 agent（关键修复！）
        if self._chat_engine:
            self._chat_engine._current_agent = self._current_agent
            logger.info(f"[_load_agent_list] Synced ChatEngine._current_agent = {self._current_agent}")
        
        self._suppress_agent_intro = False
    
    def _update_agent_button_style(self, active_agent: str):
        """更新智能体按钮样式"""
        if not hasattr(self, "_agent_buttons"):
            return
        for name, data in self._agent_buttons.items():
            btn = data["btn"]
            if name == active_agent:
                btn.setStyleSheet(data["selected_style"])
            else:
                btn.setStyleSheet(data["style"])

    def _on_agent_changed(self, agent_name: str):
        """智能体切换处理"""
        if not agent_name or not self._chat_engine:
            return
        
        logger.info(f"[_on_agent_changed] Switching from {self._current_agent} to {agent_name}")
        
        self._current_agent = agent_name
        self._chat_engine.switch_agent(agent_name)
        self._update_agent_status(agent_name)
        if not getattr(self, "_suppress_agent_intro", False):
            self._show_agent_intro(agent_name)

    def _show_agent_intro(self, agent_name: str):
        """显示智能体介绍卡片"""
        if not self._agent_manager:
            return
        agent = self._agent_manager.get_agent(agent_name)
        if not agent:
            return

        intro_md = f"""\
### 🤖 已切换到智能体：{agent.name}

{agent.description}

"""
        card = MessageCard(parent=self, role="assistant", timestamp="系统")
        card.update_content(intro_md)
        card.finish_streaming()
        self._add_chat_widget(card)
        self._scroll_to_bottom()

    def _update_agent_status(self, agent_name: str):
        """更新智能体状态显示（按钮组模式下主要更新按钮提示）"""
        if not self._agent_manager:
            return
        agent = self._agent_manager.get_agent(agent_name)
        if agent:
            mode = agent.mode
            hidden = "hidden" if agent.hidden else "visible"
            tooltip = f"{agent.name}: {agent.description}\nMode: {mode}, {hidden}"
            # 更新按钮组的 tooltip
            if hasattr(self, "_agent_buttons") and agent_name in self._agent_buttons:
                self._agent_buttons[agent_name]["btn"].setToolTip(tooltip)
            # 更新模型选择按钮的 tooltip
            if hasattr(self, "current_model_btn"):
                self.current_model_btn.setToolTip(f"{agent.name}: {agent.description}\nMode: {mode}, {hidden}")

    def _create_new_session(self):
        if self._chat_engine:
            self._chat_engine.stop()

        self._is_streaming = False
        self._tool_cancelled_by_user = False
        self._toggle_send_stop(False)

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        if self._sub_agent_floating_widget:
            self._sub_agent_floating_widget.setVisible(False)

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception(
                "Failed to auto-save current session before creating a new session"
            )

        self._cache_current_session_cards()
        session = self.session_manager.create_new_session()
        self._current_session_id = session.session_id
        self._history_preview_messages = None
        self._clear_chat_area()
        self.title_edit.setText("新对话")
        self.node_preview.clear_nodes()
        if self._todo_floating_widget:
            self._todo_floating_widget.clear()
        if self._tool_executor:
            self._tool_executor.clear_todo_list()
            self._tool_executor.set_session_context(self._current_session_id)  # 同步更新 session_id
        if self._question_floating_widget:
            self._question_floating_widget.clear()
        self._question_tool_call_id = None
        self._load_agent_list()

        QTimer.singleShot(0, self._show_initial_welcome)
        self._refresh_context_usage_indicator()

    def _display_current_session(self):
        session = self.session_manager.get_current_session()
        if not session:
            self._clear_chat_area()
            return

        self.title_edit.setText(session.topic_summary or session.name or "新对话")

        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id

        if self._restore_cached_session_cards(session):
            self._update_node_preview()
            self._refresh_context_usage_indicator()
            # 恢复缓存卡片后，多次滚动确保在底部
            self._scroll_to_bottom(sticky_ms=900)
            # 滚动完成后同步时间线节点到最后一个
            QTimer.singleShot(100, self._sync_node_preview_to_last)
            return

        self._clear_chat_area()
        self._message_batch = group_messages_for_display(session.messages)
        self._visible_batch_end = len(self._message_batch)
        self._visible_batch_start = max(
            0, self._visible_batch_end - self._initial_visible_batch_count
        )

        if not self._message_batch:
            self._show_initial_welcome()
            return

        self._load_message_batch(initial=True)

    def _show_initial_welcome(self):
        """仅在UI上显示欢迎卡片，不改动Session数据"""
        self._clear_chat_area(delete_widgets=False)
        welcome_card = self._get_or_create_welcome_card()
        self._displayed_session_id = None
        self._add_chat_widget(welcome_card)

    def _hide_welcome_cards(self):
        """隐藏所有欢迎卡片"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if getattr(widget, "_is_welcome", False):
                    widget.hide()

    def _load_message_batch(self, initial: bool = False):
        """按当前可见窗口加载消息。"""
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
            # 关键修复：同步 _current_session_id 与实际显示的会话
            self._current_session_id = session.session_id

        visible_batches = self._message_batch[
            self._visible_batch_start:self._visible_batch_end
        ]
        self._suspend_auto_scroll = not initial
        try:
            self._render_message_to_card(
                visible_batches,
                batch_offset=self._visible_batch_start,
            )
        finally:
            self._suspend_auto_scroll = False

        # 延迟滚动，确保卡片渲染完成后再滚动到底部
        # 使用多次滚动确保卡片高度变化后仍能保持在底部
        self._scroll_to_bottom(sticky_ms=900)
        self._update_node_preview()
        # 滚动完成后同步时间线节点到最后一个（延迟执行确保卡片渲染完成）
        QTimer.singleShot(100, self._sync_node_preview_to_last)
        self._refresh_context_usage_indicator()

    def _has_more_history_batches(self) -> bool:
        return self._visible_batch_start > 0

    def _load_more_history_batches(self):
        if self._is_loading_history_batches or not self._has_more_history_batches():
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        previous_value = scroll_bar.value()
        previous_height = self.chat_container.sizeHint().height()

        new_start = max(
            0, self._visible_batch_start - self._incremental_visible_batch_count
        )
        prepend_batches = self._message_batch[new_start:self._visible_batch_start]
        if not prepend_batches:
            return

        self._is_loading_history_batches = True
        self._visible_batch_start = new_start
        self._render_message_to_card(
            prepend_batches,
            insert_at_top=True,
            batch_offset=new_start,
        )
        self._sync_node_preview_to_scroll()

        def restore_anchor():
            try:
                new_height = self.chat_container.sizeHint().height()
                scroll_bar.setValue(previous_value + max(0, new_height - previous_height))
            finally:
                self._is_loading_history_batches = False

        QTimer.singleShot(0, restore_anchor)

    def _initialize_history_manager(self):
        canvas_name = getattr(self.homepage, "workflow_name", "default") or "default"
        self.history_manager = HistoryManager(canvas_name)
        # 从配置加载上次选中的项目
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        self._current_project = cfg.current_project.value
        self._project_label.setText(self._current_project)

    def _restore_latest_session(self) -> bool:
        if not self.history_manager:
            logger.info("[DEBUG] _restore_latest_session: no history_manager")
            return False

        latest = self.history_manager.load_most_recently_updated_session()
        if not latest:
            logger.info(
                "[DEBUG] _restore_latest_session: no most recently updated session"
            )
            return False

        messages = latest.get("messages", [])
        if not messages:
            logger.info("[DEBUG] _restore_latest_session: no messages")
            return False

        session_id = latest.get("session_id", "")
        restored = ChatSession.from_dict(
            {
                "session_id": session_id,
                "name": latest.get("title") or latest.get("name") or "最近会话",
                "messages": messages,
                "topic_summary": latest.get("title", ""),
                "compaction_state": latest.get("compaction_state", {}),
                "compaction_cache": latest.get("compaction_cache", {}),
                "created_at": latest.get("created_at"),
                "last_updated": latest.get("last_updated"),
            }
        )
        self.session_manager.set_current_session(restored)
        self._history_preview_messages = None
        self._current_session_id = session_id
        self.title_edit.setText(latest.get("title") or "最近会话")
        # 恢复项目
        project = latest.get("project", "默认项目") or "默认项目"
        self._current_project = project
        self._project_label.setText(project)
        self._load_agent_list()
        if self._tool_executor:
            self._tool_executor.set_session_context(self._current_session_id)
        self._display_current_session()
        self._refresh_context_usage_indicator()
        return True

    def _open_diff_viewer(self):
        """打开差异查看窗口，显示当前会话修改文件的 git diff"""
        try:
            # 获取当前会话 ID
            session_id = self._current_session_id
            if not session_id:
                InfoBar.warning("提示", "当前没有活动会话", parent=self)
                return

            # 从 ToolExecutor 获取当前会话的文件操作记录
            if not self._tool_executor:
                InfoBar.warning("提示", "工具执行器未初始化", parent=self)
                return

            # 获取文件操作记录
            from app.utils.file_operation_recorder import FileOperationRecorder
            from app.utils.session_store import SessionStore

            session_store = SessionStore()
            file_recorder = FileOperationRecorder(session_store)

            # 获取当前会话的所有文件操作
            operations = file_recorder.get_all_operations_for_session(session_id)

            if not operations:
                InfoBar.info("提示", "当前会话没有文件修改记录", parent=self)
                return

            # 提取文件路径列表（去重）
            file_paths = list({op.get("file_path") for op in operations if op.get("file_path")})

            if not file_paths:
                InfoBar.info("提示", "未找到修改的文件", parent=self)
                return

            # 生成 git diff
            try:
                diff_output = DiffHtmlGenerator.get_diff_for_files(file_paths, session_id)
            except Exception as e:
                logger.warning(f"[DiffViewer] 获取 git diff 失败: {e}")
                diff_output = ""

            # 生成 HTML 报告
            html = DiffHtmlGenerator.generate_html_report(diff_output or "", session_id)

            # 创建并显示差异查看窗口
            viewer = DiffViewerWindow(parent=self)
            viewer.load_html(html)
            viewer.show()

            logger.info(f"[DiffViewer] 已打开差异查看窗口，文件数: {len(file_paths)}")

        except ImportError as e:
            logger.error(f"[DiffViewer] 导入模块失败: {e}")
            InfoBar.error("错误", f"功能加载失败: {str(e)}", parent=self)
        except Exception as e:
            logger.exception(f"[DiffViewer] 打开差异查看器失败: {e}")
            InfoBar.error("错误", f"打开差异查看器失败: {str(e)}", parent=self)

    def _clear_chat_area(self, delete_widgets: bool = True):
        self._current_assistant_card = None
        self._displayed_session_id = None
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item.widget():
                if delete_widgets:
                    item.widget().deleteLater()
                else:
                    item.widget().hide()

    def _take_chat_widgets(self) -> List[QWidget]:
        widgets: List[QWidget] = []
        self._current_assistant_card = None
        self._displayed_session_id = None
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                widgets.append(item.widget())
        return widgets

    def _cache_current_session_cards(self):
        session = self.session_manager.get_current_session()
        widgets = self._take_chat_widgets()
        if not session or not session.messages:
            return

        message_cards = [
            w
            for w in widgets
            if isinstance(w, MessageCard) and self._is_widget_alive(w)
        ]
        if message_cards:
            self._session_card_cache[session.session_id] = {
                "cards": message_cards,
                "visible_batch_start": self._visible_batch_start,
                "visible_batch_end": self._visible_batch_end,
                "batch_count": len(group_messages_for_display(session.messages)),
            }

        self._cleanup_session_card_cache()

    def _cleanup_session_card_cache(self):
        from app.constants import (
            MAX_SESSION_CARD_CACHE_SIZE,
        )
        all_session_ids = {
            s.session_id for s in self.session_manager.get_all_sessions()
        }
        cleanup_stale_card_cache(
            self._session_card_cache,
            all_session_ids,
            MAX_SESSION_CARD_CACHE_SIZE
        )

    def _is_widget_alive(self, widget: Optional[QWidget]) -> bool:
        """检查 widget 是否存活（保留向后兼容）"""
        return is_widget_alive(widget)

    def _restore_cached_session_cards(self, session: ChatSession) -> bool:
        if not session.messages:
            return False

        cache_entry = self._session_card_cache.get(session.session_id)
        if not cache_entry:
            return False
        cached_cards = cache_entry.get("cards") if isinstance(cache_entry, dict) else None
        if not cached_cards:
            return False
        batch_count = len(group_messages_for_display(session.messages))
        if cache_entry.get("batch_count") != batch_count:
            self._session_card_cache.pop(session.session_id, None)
            return False

        alive_cards, removed = filter_alive_cards(cached_cards)
        if removed:
            self._session_card_cache.pop(session.session_id, None)
        if not alive_cards:
            return False

        self._clear_chat_area(delete_widgets=False)
        for card in alive_cards:
            self._add_chat_widget(card)
        self._displayed_session_id = session.session_id
        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id
        self._current_assistant_card = (
            alive_cards[-1]
            if alive_cards and alive_cards[-1].role == "assistant"
            else None
        )
        self._message_batch = group_messages_for_display(session.messages)
        self._visible_batch_start = max(
            0, int(cache_entry.get("visible_batch_start", 0))
        )
        self._visible_batch_end = min(
            len(self._message_batch),
            int(cache_entry.get("visible_batch_end", len(self._message_batch))),
        )
        return True

    def _get_or_create_welcome_card(self) -> MessageCard:
        agent = (
            self._agent_manager.get_agent(self._current_agent)
            if self._agent_manager
            else None
        )
        agent_name = agent.name if agent else ""
        agent_desc = agent.description if agent else ""
        
        # 获取最近会话和最多消息的会话用于欢迎卡片（按当前项目过滤）
        history_list = self.history_manager.get_history_list(self._current_project)
        
        # 最近会话（按时间排序，取前3）
        recent_sessions = []
        for session in history_list[:3]:
            recent_sessions.append({
                "title": session.get("title"),
                "last_time": session.get("last_time"),
                "session_id": session.get("session_id"),
                "message_count": session.get("message_count", 0),
            })
        
        # 最多消息的会话（按消息数量排序，取前3）
        top_sessions = sorted(history_list, key=lambda x: x.get("message_count", 0), reverse=True)[:3]
        top_by_count = []
        for session in top_sessions:
            top_by_count.append({
                "title": session.get("title"),
                "last_time": session.get("last_time"),
                "session_id": session.get("session_id"),
                "message_count": session.get("message_count", 0),
            })
        
        # 每次都重新创建，确保会话列表是最新的
        welcome_card = create_welcome_card(
            self, agent_name, agent_desc, recent_sessions, top_by_count
        )
        welcome_card._is_welcome = True
        welcome_card.contextActionRequested.connect(
            self.handle_recommended_question
        )
        return welcome_card

    def _sanitize_user_message_for_display(self, content: str) -> str:
        """清理用户消息用于显示（保留向后兼容）"""
        return sanitize_user_message_for_display(content)

    def _get_user_round_index_for_batch_index(
        self, batch_index: int, batch_offset: int = 0
    ) -> int:
        """
        计算给定 batch 索引对应的 round_index（user 轮次索引）

        逻辑：
        - 对于 user batch：round_index = 前面有多少个 user batch
        - 对于 assistant batch：round_index = 前面有多少个 user batch - 1

        Args:
            batch_index: batch 在 _message_batch 中的索引
            batch_offset: 当前加载批次的起始偏移量（用于分批加载历史消息）

        Returns:
            round_index：从 0 开始的用户轮次索引
        """
        global_batch_index = batch_index
        
        # 统计 global_batch_index 之前有多少个 user batch
        user_count = 0
        for idx in range(global_batch_index):
            if idx >= len(self._message_batch):
                break
            batch = self._message_batch[idx]
            if batch and batch[0].get("role") == "user":
                user_count += 1
        
        # 对于 assistant batch，round_index 需要减 1
        # 这是因为 assistant 属于它前面那个 user 的 round
        current_batch = None
        if global_batch_index < len(self._message_batch):
            current_batch = self._message_batch[global_batch_index]
        
        if current_batch and current_batch[0].get("role") != "user":
            user_count = max(0, user_count - 1)
        
        return user_count

    def _render_message_to_card(
        self,
        batches: List[List[Dict[str, Any]]],
        insert_at_top: bool = False,
        batch_offset: int = 0,
    ):
        insert_index = 0 if insert_at_top else None
        for local_index, batch in enumerate(batches):
            role = batch[0].get("role")
            timestamp = batch[0].get("timestamp") or get_default_timestamp()
            global_batch_index = batch_offset + local_index
            round_index = self._get_user_round_index_for_batch_index(
                global_batch_index, batch_offset
            )

            if role == "user":
                content = self._sanitize_user_message_for_display(
                    batch[0].get("content", "")
                )
                user_card = self._append_user_message(
                    content,
                    timestamp=timestamp,
                    tag_params=batch[0].get("params", {}),
                    scroll=False,
                    insert_index=insert_index,
                    user_round_index=round_index,
                    update_preview=not insert_at_top,
                )
                if user_card:
                    # 设置 message_index 用于卡片差异功能
                    user_card._message_index = global_batch_index
                if insert_index is not None and user_card:
                    insert_index += 1

            if role == "assistant" or role == "tool":
                assistant_card = self._append_assistant_message(
                    timestamp=timestamp,
                    scroll=False,
                    insert_index=insert_index,
                    round_index=round_index,
                )
                if assistant_card:
                    # 设置 message_index 用于卡片差异功能
                    assistant_card._message_index = global_batch_index
                # 使用辅助函数渲染消息
                render_batch_to_assistant_card(assistant_card, batch)
                if insert_index is not None and assistant_card:
                    insert_index += 1

    def _get_rendered_message_cards(self) -> List[MessageCard]:
        def is_user_or_assistant(widget):
            return widget.role in ("user", "assistant")
        return collect_message_cards_from_layout(self.chat_layout, is_user_or_assistant)

    def _get_current_user_round_index(self) -> int:
        """获取当前 user message 应该是第几个 user（从 0 开始）"""
        return count_user_cards_in_layout(self.chat_layout)

    def _find_user_round_index_for_card(self, card: MessageCard) -> Optional[int]:
        """
        通过遍历布局找到 user card 对应的 round_index
        """
        # 直接通过布局遍历确定位置（更可靠）
        user_card_idx = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget is card:
                return user_card_idx
            if widget.role == "user":
                user_card_idx += 1
        return None

    def findRoundIndexForCard(self, card: MessageCard) -> Optional[int]:
        """
        供 MessageCard 回调使用，根据 assistant card 查找对应的 round_index。
        通过遍历布局找到该 assistant card 前面的 user card 数量来确定 round_index。
        """
        if not card or card.role != "assistant":
            return None
        # 遍历布局，统计该 assistant card 之前有多少 user card
        round_index = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if widget is card:
                # 找到了，返回当前 round_index
                return round_index
            if widget.role == "user":
                round_index += 1
        return None

    def _find_user_round_index_from_session(
        self,
        session,
        user_text: str,
        timestamp: str,
    ) -> Optional[int]:
        """
        从 session 数据中找到 user 消息对应的 round_index。

        通过在 session.messages 中定位 user 消息，然后计算它是第几个 user。

        Args:
            session: ChatSession 对象
            user_text: 用户消息的纯文本内容
            timestamp: 用户消息的时间戳

        Returns:
            round_index 或 None
        """
        from app.utils.message_content import consolidate_messages

        canonical_messages = consolidate_messages(session.messages)
        user_count_total = sum(1 for msg in canonical_messages if msg.get("role") == "user")

        # 规范化时间戳进行比较（去掉秒）
        # MessageCard 的 timestamp 格式是 "YYYY-MM-DD HH:MM"（无秒）
        # session.messages 的 timestamp 格式是 "YYYY-MM-DD HH:MM:SS"（有秒）
        card_ts_prefix = timestamp[:16] if timestamp else ""

        logger.debug(f"[UNDO] Searching for user message: card_ts={card_ts_prefix}, content_len={len(user_text)}")
        logger.debug(f"[UNDO] Session has {len(canonical_messages)} messages, {user_count_total} user messages")

        # 在消息列表中查找匹配的 user 消息
        # 关键修复：同时匹配时间戳+内容，避免多条消息匹配到同一条
        user_count = 0
        for msg in canonical_messages:
            if msg.get("role") == "user":
                msg_content = msg.get("content", "")
                msg_timestamp = msg.get("timestamp", "") or ""
                msg_ts_prefix = msg_timestamp[:16]

                # 同时匹配时间戳和内容，确保唯一性
                # 时间戳精确到分钟，内容完全匹配
                if msg_ts_prefix == card_ts_prefix and msg_content == user_text:
                    logger.info(
                        f"[UNDO] Matched user message: user_count={user_count}, "
                        f"ts={card_ts_prefix}, content_len={len(user_text)}"
                    )
                    return user_count
                user_count += 1

        # 兜底：如果时间戳+内容都没匹配到，尝试内容匹配（兼容旧数据）
        user_count = 0
        for msg in canonical_messages:
            if msg.get("role") == "user":
                msg_content = msg.get("content", "")
                if msg_content == user_text:
                    logger.warning(
                        f"[UNDO] Fallback match by content only: user_count={user_count}, "
                        f"content_len={len(user_text)}"
                    )
                    return user_count
                user_count += 1

        # 调试：显示所有 user 消息的时间戳，帮助诊断
        logger.warning(f"[UNDO] No match found for user message. Total user messages: {user_count_total}")
        if user_count_total > 0:
            logger.warning("[UNDO] Available user messages in session:")
            for i, msg in enumerate(canonical_messages):
                if msg.get("role") == "user":
                    msg_ts = (msg.get("timestamp", "") or "")[:16]
                    msg_content_preview = (msg.get("content", "") or "")[:50]
                    logger.warning(f"[UNDO]   [{i}] ts={msg_ts}, content={msg_content_preview}...")

        return None

    def _remove_cards_for_round(self, round_index: int) -> bool:
        session = self.session_manager.get_current_session()
        if not session:
            return False

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            return False

        start_idx, end_idx = round_ranges[round_index]
        cards_to_remove = end_idx - start_idx

        user_card_idx = 0
        removed = 0
        removing = False
        widgets_to_remove = []

        # 遍历 chat_layout
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role not in ("user", "assistant"):
                continue

            if widget.role == "user":
                if user_card_idx == round_index:
                    widgets_to_remove.append(widget)
                    removed += 1
                    removing = True
                else:
                    removing = False
                user_card_idx += 1
            elif widget.role == "assistant" and removing:
                widgets_to_remove.append(widget)
                removed += 1

            if removed >= cards_to_remove:
                break

        logger.info(
            f"[DELETE] Cards to remove: {len(widgets_to_remove)}, cards_to_remove: {cards_to_remove}"
        )

        # 使用辅助函数执行删除
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        return removed > 0

    def _remove_cards_from_round(self, round_index: int) -> bool:
        """从指定 round 开始删除所有卡片（包括后续卡片）"""
        # 计算预期删除的卡片数量
        session = self.session_manager.get_current_session()
        cards_to_remove_hint = 0
        if session:
            from app.utils.message_content import consolidate_messages, get_user_round_ranges
            canonical_messages = consolidate_messages(session.messages)
            round_ranges = get_user_round_ranges(canonical_messages)
            if round_index < len(round_ranges):
                start_idx, end_idx = round_ranges[round_index]
                cards_to_remove_hint = end_idx - start_idx
        
        widgets_to_remove = find_widgets_to_remove_from_round(
            self.chat_layout, round_index, cards_to_remove_hint
        )
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        
        # 关键修复：如果 UI 删除的卡片数量少于预期，清空整个聊天区域并重新渲染
        if cards_to_remove_hint > 0 and len(widgets_to_remove) < cards_to_remove_hint:
            from loguru import logger
            logger.warning(
                f"[UNDO] UI cards incomplete: deleting {len(widgets_to_remove)}/{cards_to_remove_hint}. "
                f"Clearing and re-rendering session view."
            )
            self._clear_chat_area()
            self._display_current_session()
            return False
        return len(widgets_to_remove) > 0

    def _invalidate_current_session_card_cache(self):
        invalidate_session_card_cache(
            self.session_manager.get_current_session(),
            self._session_card_cache
        )

    def _persist_session_after_mutation(self):
        session = self.session_manager.get_current_session()
        if not session:
            return

        session.set_messages(session.messages, preserve_compaction=False)

        if is_session_empty(session):
            if self._current_session_id is not None and self.history_manager:
                idx = self.history_manager.find_index_by_session_id(
                    self._current_session_id
                )
                if idx is not None:
                    self.history_manager.archive_history(idx)
                self._current_session_id = None
            return

        if self.history_manager:
            # 使用辅助函数保存会话
            self._current_session_id = save_or_archive_session(
                self.history_manager,
                session,
                self._current_session_id
            )

    def _refresh_session_view_after_mutation(self):
        # 使用辅助函数刷新视图
        refresh_session_view(
            self,
            self._invalidate_current_session_card_cache,
            self._display_current_session,
            self._refresh_context_usage_indicator
        )

    def _sync_current_assistant_card_ref(self):
        self._current_assistant_card = find_last_assistant_card(self.chat_layout)

    def _finalize_local_session_mutation(self):
        self._invalidate_current_session_card_cache()
        self._history_preview_messages = None
        session = self.session_manager.get_current_session()
        
        if is_session_empty(session):
            self._clear_chat_area()
            self.node_preview.clear_nodes()
            self._current_assistant_card = None
            self._show_initial_welcome()
            self._refresh_context_usage_indicator()
            return

        self._sync_current_assistant_card_ref()
        self._update_node_preview()
        self._refresh_context_usage_indicator()
        self._sync_node_preview_to_scroll()

    def _on_clear_shortcut(self):
        # 使用辅助函数清空并显示欢迎
        clear_and_show_welcome(
            session=self.session_manager.get_current_session(),
            session_card_cache=self._session_card_cache,
            clear_chat_func=self._clear_chat_area,
            clear_preview_func=self.node_preview.clear_nodes,
            get_welcome_func=self._get_or_create_welcome_card,
            add_widget_func=lambda w: QTimer.singleShot(0, lambda: self._add_chat_widget(w))
        )
        self.title_edit.setText("新对话")

    def _add_chat_widget(self, widget: QWidget, insert_index: Optional[int] = None):
        if insert_index is None:
            add_message_to_layout(widget, self.chat_layout, is_widget_alive)
        else:
            if is_widget_alive(widget):
                widget.setParent(self.chat_container)
                if isinstance(widget, MessageCard) and widget.role == "user":
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignRight)
                elif isinstance(widget, MessageCard):
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignLeft)
                else:
                    self.chat_layout.insertWidget(insert_index, widget)
                widget.show()
        if isinstance(widget, MessageCard):
            try:
                widget.heightChanged.disconnect(self._on_message_card_height_changed)
            except Exception:
                pass
            widget.heightChanged.connect(self._on_message_card_height_changed)
            if self._resize_preview_active:
                widget.set_resize_preview_mode(True)
            widget.sync_width()

    def _archive_history_session(self, index: int):
        history_list = self.history_manager.get_history_list(self._current_project)
        if index < 0 or index >= len(history_list):
            return

        target_session_id = history_list[index].get("session_id")
        archived_current = (
            self._current_session_id is not None
            and target_session_id == self._current_session_id
        )

        old_session_manager = self.session_manager
        old_chat_engine = self._chat_engine

        # 清理归档会话的文件操作记录和备份
        if self._tool_executor and self._tool_executor.file_recorder:
            self._tool_executor.file_recorder.clear_session(target_session_id)
            logger.info(f"[FileRecorder] 已清理归档会话的文件操作记录: {target_session_id}")

        archived = self.history_manager.archive_history(index)

        if archived_current and archived:
            # 使用辅助函数创建新会话状态
            new_state = create_new_session_state(old_session_manager, old_chat_engine)
            init_new_session_after_archive(
                self, new_state, self._tool_executor,
                self._clear_chat_area, self._show_initial_welcome
            )

        # 刷新历史会话卡片
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _rename_history_session(self, index: int, new_title: str):
        if not self.history_manager:
            return
        history_list = self.history_manager.get_history_list(self._current_project)
        if 0 <= index < len(history_list):
            session_record = history_list[index]
            session_id = session_record.get("session_id")
            if session_id:
                session = self.history_manager.get_session_by_session_id(session_id)
                if session:
                    idx = self.history_manager.find_index_by_session_id(session_id)
                    if idx is not None:
                        self.history_manager.update_session_title(idx, new_title)
        # 刷新历史会话卡片
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _on_session_imported(self, data: dict):
        """处理导入的会话文件"""
        if not self.history_manager:
            return

        file_path = data.get("file_path")
        if not file_path:
            return

        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 刷新历史会话卡片
            self._refresh_history_toggle_panel()
            # 显示提示信息
            InfoBar.success(
                title="导入成功",
                content=f"已导入会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )
            logger.info(f"[导入会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="导入失败",
                content="无法解析会话文件，请确认文件格式正确",
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )
            logger.warning(f"[导入会话] 失败: {file_path}")

    def _on_archived_session_restored(self, file_path: str):
        """恢复归档会话到历史会话"""
        if not self.history_manager:
            return

        # 导入归档的会话
        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 删除归档文件
            try:
                import os
                os.remove(file_path)
                logger.info(f"[恢复会话] 已删除归档文件: {file_path}")
            except Exception as e:
                logger.warning(f"[恢复会话] 删除归档文件失败: {e}")

            # 刷新归档列表
            self._refresh_archived_sessions()
            
            InfoBar.success(
                title="恢复成功",
                content=f"已恢复会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )
            logger.info(f"[恢复会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="恢复失败",
                content="无法恢复该会话，文件可能已损坏",
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )

    def _on_archived_session_deleted(self, file_path: str):
        """彻底删除归档会话"""
        from qfluentwidgets import MessageBox
        
        # 确认对话框
        msg_box = MessageBox(
            "确认删除",
            "确定要彻底删除这个归档会话吗？此操作不可恢复。",
            self
        )
        msg_box.yesButton.setText("删除")
        msg_box.cancelButton.setText("取消")
        
        if msg_box.exec() != MessageBox.Accepted:
            return

        try:
            import os
            os.remove(file_path)
            logger.info(f"[彻底删除] 成功: {file_path}")
            
            # 刷新归档列表
            self._refresh_archived_sessions()
            
            InfoBar.success(
                title="删除成功",
                content="归档会话已彻底删除",
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
        except Exception as e:
            logger.error(f"[彻底删除] 失败: {e}")
            InfoBar.error(
                title="删除失败",
                content=f"无法删除文件：{str(e)}",
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )

    def _on_archived_session_renamed(self, file_path: str, new_title: str):
        """重命名归档会话"""
        try:
            import json
            # 读取文件
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 更新标题
            data["title"] = new_title
            
            # 写回文件
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[归档会话重命名] 成功: {file_path} -> {new_title}")
            
            # 刷新归档列表
            self._refresh_archived_sessions()
            
            InfoBar.success(
                title="重命名成功",
                content=f"已更名为：{new_title}",
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self
            )
        except Exception as e:
            logger.error(f"[归档会话重命名] 失败: {e}")
            InfoBar.error(
                title="重命名失败",
                content=str(e),
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self
            )

    def _load_history_session(self, index: int):
        self._load_history_session_from_popup(index)

    def _load_history_session_from_popup(self, index: int):
        if self._is_streaming:
            self._on_stop_clicked()

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception(
                "Failed to auto-save current session before loading history"
            )

        self._tool_executor.reset_session_state()

        history_list = self.history_manager.get_history_list(self._current_project)
        if index < 0 or index >= len(history_list):
            return

        session_record = history_list[index]
        session_id = session_record.get("session_id")
        # 通过 session_id 获取会话消息，而非通过 index
        messages = self.history_manager.get_session_messages(session_id)
        if not messages:
            return

        title = session_record.get("title") or session_record.get("name") or "历史对话"
        
        # 使用辅助函数创建会话
        restored = create_session_from_record(session_record, messages, title)
        
        # 使用辅助函数初始化
        init_after_loading_session(
            self, restored, session_id, title,
            self._tool_executor
        )

        # 如果会话有自己的项目，显示在标题上
        session_project = session_record.get("project", "默认项目") or "默认项目"
        self._current_project = session_project
        self._project_label.setText(session_project)

        self._display_current_session()

        # 刷新历史会话卡片
        if self._history_card.isVisible():
            self._refresh_history_toggle_panel()

    def _append_user_message(
        self,
        content: str,
        timestamp: str = None,
        tag_params: dict = None,
        scroll: bool = True,
        insert_index: Optional[int] = None,
        user_round_index: Optional[int] = None,
        update_preview: bool = True,
    ):
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
        
        # 计算当前 user message 的 round_index
        if user_round_index is None:
            user_round_index = self._get_current_user_round_index()
        
        card = MessageCard(
            parent=self,
            role="user",
            timestamp=timestamp,
            tag_params=tag_params
            or {},
        )
        card._round_index = user_round_index
        card.update_content(content)
        card.finish_streaming()
        
        # 设置卡片信号
        setup_user_card_signals(card, self._delete_message, self._undo_from_message, self._on_code_action)
        
        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()

        # 使用辅助函数后处理
        post_append_user_message(
            self,
            user_round_index,
            self._update_node_preview if update_preview else None,
        )
        return card

    def _append_assistant_message(
        self,
        timestamp: str = None,
        scroll: bool = True,
        insert_index: Optional[int] = None,
        round_index: Optional[int] = None,
    ) -> MessageCard:
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
            
        # 使用辅助函数创建卡片
        def on_context_action(action, context):
            self.handle_recommended_question(action, context)
            if hasattr(self.homepage, "on_context_action"):
                self.homepage.on_context_action(action, context)
            else:
                self.contextActionRequested.emit(action, context)
        
        card = create_assistant_card_widget(
            parent=self,
            timestamp=timestamp,
            round_index=(
                round_index
                if round_index is not None
                else self._current_assistant_round_index
            ),
            on_action=self._on_code_action,
            on_context_action=on_context_action,
            on_tool_diff=self._on_tool_diff_requested,
            on_card_diff=self._on_card_diff_requested,
            on_save_file=self._on_save_file_requested,
            on_subagent_log=self._on_subagent_log_requested,
        )
        
        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()
        return card

    def _update_assistant_message(self, card: MessageCard, new_content: str):
        card.update_content(new_content)
        scroll_to_bottom_if_streaming(self.chat_scroll_area, self._is_streaming)

    def _update_node_preview(self):
        session = self.session_manager.get_current_session()
        if not session:
            return
        
        # 使用辅助函数构建 node preview 数据
        node_data = build_node_preview_from_session(session, content_to_text, max_len=30)

        self.node_preview.update_nodes(node_data)
        self._sync_node_preview_to_scroll()

    def _sync_node_preview_to_scroll(self):
        # 加载历史时抑制滚动同步，避免节点跑到渲染的卡片数量位置
        if self._suppress_scroll_sync_count > 0:
            self._suppress_scroll_sync_count -= 1
            return
        
        if not hasattr(self, "chat_scroll_area") or not hasattr(self, "node_preview"):
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 使用辅助函数收集用户卡片
        user_widgets = collect_user_card_widgets(self.chat_layout)

        if not user_widgets:
            self._last_visible_user_pair_index = -1
            self.node_preview.set_visible_node(-1)
            self.node_preview.set_progress_position(-1)
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        viewport_height = self.chat_scroll_area.viewport().height()
        visible_top = scroll_bar.value()
        
        # 关键修复：基于滚动比例计算进度，而非渲染的卡片数量
        # 这样即使只渲染了部分卡片，进度条也能正确显示
        scroll_max = scroll_bar.maximum()
        if scroll_max > 0:
            # 计算滚动比例（0.0 到 1.0）
            scroll_ratio = visible_top / scroll_max
            # 计算对应的节点索引
            total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 1
            progress = scroll_ratio * (total_nodes - 1)
        else:
            progress = 0.0
        
        # 计算可见的 user card 索引（用于高亮）
        # 关键修复：高亮索引基于滚动比例计算，而非渲染的卡片位置
        # 这样动态加载时高亮也是正确的
        user_tops = [widget.y() for widget in user_widgets]
        
        # 使用辅助函数计算滚动进度（用于进度条）
        _, _ = calculate_scroll_progress(
            visible_top, viewport_height, user_tops
        )

        # 只在节点数量范围内更新
        total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 0
        if total_nodes > 0:
            # 如果有待更新的目标索引（点击跳转），直接使用它
            if self._pending_scroll_to_update is not None:
                highlighted_index = self._pending_scroll_to_update
                self._pending_scroll_to_update = None
            else:
                # 正常滚动时：高亮索引跟随滚动比例计算
                clamped_progress = min(max(progress, 0.0), total_nodes - 1)
                highlighted_index = int(round(clamped_progress))
            
            highlighted_index = max(0, min(highlighted_index, total_nodes - 1))
            
            # 更新进度条（使用索引作为 progress）
            self.node_preview.set_progress_position(highlighted_index)
            
            if highlighted_index != self._last_visible_user_pair_index:
                self._last_visible_user_pair_index = highlighted_index
                self.node_preview.set_visible_node(highlighted_index)

    def _sync_node_preview_to_last(self):
        """滚动完成后同步到最后一个节点"""
        if not hasattr(self, "node_preview"):
            return
        
        # 使用 message_batch 计算实际的最后一个 user message 索引
        # 而不是依赖当前渲染的卡片数量（可能只渲染了部分）
        session = self.session_manager.get_current_session()
        if session:
            # 从 session 消息计算所有 user 数量
            user_count = sum(
                1 for msg in session.messages 
                if msg.get("role") == "user"
            )
            if user_count > 0:
                last_index = user_count - 1
                self.node_preview.set_visible_node(last_index)
                self.node_preview.set_progress_position(last_index)
                self._last_visible_user_pair_index = last_index

    def _scroll_to_target_node_index(self, target_index: int):
        """
        滚动到指定节点索引的位置。如果目标卡片未渲染，先加载历史批次。

        Args:
            target_index: 目标节点索引（0-based）
        """
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 计算目标 user 在 _message_batch 中的 batch 索引
        # 找到 _message_batch 中第 target_index 个 user batch
        target_batch_index = -1
        user_count = 0
        for idx, batch in enumerate(self._message_batch):
            if batch and batch[0].get("role") == "user":
                if user_count == target_index:
                    target_batch_index = idx
                    break
                user_count += 1

        if target_batch_index < 0:
            logger.warning(f"[NodePreview] Cannot find batch for target_index={target_index}")
            return

        # 检查目标 batch 是否已经加载（可见）
        if target_batch_index >= self._visible_batch_start:
            # 已加载，直接滚动到目标位置
            self._scroll_to_batch_index(target_batch_index)
            return

        # 需要加载更多历史批次
        # 计算需要加载的起始位置（预留一些缓冲批次）
        new_start = max(0, target_batch_index - self._incremental_visible_batch_count // 2)
        
        # 标记要滚动的目标索引，以便加载完成后使用
        self._pending_scroll_to_index = target_index
        self._pending_scroll_to_batch = target_batch_index

        # 加载历史批次到目标位置
        self._load_history_to_index(new_start)

    def _load_history_to_index(self, target_batch_start: int):
        """
        加载历史批次直到到达目标 batch 起始位置

        Args:
            target_batch_start: 目标批次起始索引
        """
        if target_batch_start >= self._visible_batch_start:
            # 已经到达目标位置，滚动到目标节点
            self._scroll_to_pending_target()
            return

        # 计算需要加载多少批次
        batch_count = self._visible_batch_start - target_batch_start
        if batch_count > 0:
            # 触发分批加载
            self._render_message_to_card(
                self._message_batch[target_batch_start:self._visible_batch_start],
                insert_at_top=True,
                batch_offset=target_batch_start,
            )

            # 更新可见范围
            self._visible_batch_start = target_batch_start

            # 延迟检查是否需要继续加载（使用 QTimer.singleShot 避免重复）
            QTimer.singleShot(100, lambda: self._load_history_to_index(target_batch_start))

    def _scroll_to_pending_target(self):
        """滚动到待处理的目标节点"""
        if self._pending_scroll_to_index is None:
            return

        target_index = self._pending_scroll_to_index
        self._pending_scroll_to_index = None
        self._pending_scroll_to_batch = None

        # 先标记目标索引（但不直接设置进度条）
        # 让 _sync_node_preview_to_scroll 在滚动时自动计算正确的值
        total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 0
        if total_nodes > 0:
            self._pending_scroll_to_update = target_index
        
        # 现在目标卡片应该已经渲染，尝试找到它并滚动
        target_widget = find_user_card_at_index(self.chat_layout, target_index)
        if target_widget:
            # 滚动到目标位置，_sync_node_preview_to_scroll 会自动更新高亮和进度
            self.chat_scroll_area.verticalScrollBar().setValue(target_widget.y())
        else:
            # 仍然找不到，说明批次数量不足，继续加载
            logger.info(f"[NodePreview] Still not found, target_index={target_index}")

    def _scroll_to_batch_index(self, batch_index: int):
        """
        滚动到指定 batch 索引的位置

        Args:
            batch_index: 目标 batch 索引
        """
        # 标记目标索引
        total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 0
        if total_nodes > 0:
            self._pending_scroll_to_update = batch_index
        
        # 找到对应的 user card widget 并滚动
        target_widget = find_user_card_at_index(self.chat_layout, batch_index)
        if target_widget:
            # 滚动到目标位置，_sync_node_preview_to_scroll 会自动更新高亮和进度
            self.chat_scroll_area.verticalScrollBar().setValue(target_widget.y())

    def _on_node_preview_clicked(self, index: int):
        # 使用辅助函数找到目标卡片
        target_widget = find_user_card_at_index(self.chat_layout, index)

        if target_widget:
            # 标记目标索引，让 _sync_node_preview_to_scroll 自动计算正确的值
            total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 0
            if total_nodes > 0:
                self._pending_scroll_to_update = index
            # 滚动到目标位置，_sync_node_preview_to_scroll 会自动更新高亮和进度
            self.chat_scroll_area.verticalScrollBar().setValue(target_widget.y())
        else:
            # 关键修复：目标卡片未渲染（动态加载）
            # 计算需要加载多少历史批次才能到达该索引
            self._scroll_to_target_node_index(index)

    def _on_scroll_changed(self, value):
        self._sync_node_preview_to_scroll()
        if self._bottom_anchor_deadline > 0:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            if value < scroll_bar.maximum():
                self._bottom_anchor_deadline = 0.0
                self._bottom_anchor_timer.stop()
        if value <= self._history_load_threshold:
            self._load_more_history_batches()
        # 滚动时复用单个防抖定时器，避免堆积大量 singleShot 回调
        self._scroll_sync_timer.stop()
        self._scroll_sync_timer.start()

    def _truncate_session_from_user_round(self, round_index: int, card: MessageCard = None) -> bool:
        """
        截断 session 数据到指定 round 之前，并删除 UI 卡片
        
        UI 删除策略：基于 card widget 对象在 chat_layout 中的位置精准删除，
        不依赖 round_index 遍历（解决懒加载时卡片序号对不上的问题）
        """
        from loguru import logger
        
        session = self.session_manager.get_current_session()
        if not session:
            logger.error("[UNDO] No session found")
            return False

        # === 1. 删除 UI 卡片：从 card 到末尾 ===
        if card is not None:
            # 找到 card 在 chat_layout 中的索引
            card_layout_idx = -1
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() is card:
                    card_layout_idx = i
                    break
            
            if card_layout_idx >= 0:
                # 收集要删除的 widgets：从 card 到末尾（撤销 = 删除之后所有）
                widgets_to_remove = []
                for i in range(card_layout_idx, self.chat_layout.count()):
                    item = self.chat_layout.itemAt(i)
                    if item and item.widget():
                        w = item.widget()
                        if hasattr(w, '_is_welcome') and w._is_welcome:
                            continue
                        widgets_to_remove.append(w)
                
                from app.widgets.ui_helpers import delete_widgets_from_layout
                deleted_count = delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
                logger.info(f"[UNDO] Removed {deleted_count} cards from UI")
            else:
                logger.warning("[UNDO] Card not found in layout, UI cards not deleted")
        else:
            logger.warning("[UNDO] No card provided, skipping UI deletion")
        
        # === 2. 基于 session.messages 计算截断位置 ===
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        
        if round_index < 0 or round_index >= len(round_ranges):
            logger.error(f"[UNDO] Invalid round_index: {round_index}, available: {len(round_ranges)}")
            return False
        
        cutoff_index = round_ranges[round_index][0]
        logger.info(f"[UNDO] Truncating session: round_index={round_index}, cutoff_index={cutoff_index}")
        
        # === 3. 截断 session.messages ===
        session.set_messages(
            session.messages[:cutoff_index], preserve_compaction=False
        )
        
        # === 4. 同步 _message_batch ===
        self._message_batch = group_messages_for_display(session.messages)
        
        # === 5. 保存 session ===
        self._persist_session_after_mutation()
        
        # === 6. 收尾 ===
        self._finalize_local_session_mutation()
        
        return True

    def _delete_message(self, card: MessageCard):
        if card.role != "user":
            return
        # 直接传 card 对象，不依赖 round_index 定位 UI 卡片
        self._delete_user_round(card)

    def _delete_user_round(self, card: MessageCard):
        """
        删除单个 round：找到 card 在 chat_layout 中的位置，
        删除该 user card 及其后直到下一个 user card 之间的所有卡片
        """
        from loguru import logger
        
        logger.info(f"[DELETE] Starting deletion for card at round_index={card._round_index}")
        
        # === 1. 删除 UI 卡片：基于 card widget 对象在 layout 中的位置 ===
        # 找到 card 在 chat_layout 中的索引
        card_layout_idx = -1
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() is card:
                card_layout_idx = i
                break
        
        if card_layout_idx < 0:
            logger.warning("[DELETE] Card not found in layout")
            return
        
        # 收集要删除的 widgets：从 card 开始，直到下一个 user card 或末尾
        widgets_to_remove = [card]
        for i in range(card_layout_idx + 1, self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            w = item.widget()
            # 遇到下一个 user card 就停止
            if hasattr(w, 'role') and w.role == "user" and not getattr(w, "_is_welcome", False):
                break
            widgets_to_remove.append(w)
        
        from app.widgets.ui_helpers import delete_widgets_from_layout
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        logger.info(f"[DELETE] Removed {len(widgets_to_remove)} cards from UI")
        
        # === 2. 更新 session 数据 ===
        session = self.session_manager.get_current_session()
        if not session:
            logger.error("[DELETE] No session found")
            return
        
        # 从 card._round_index 计算 session 截断位置
        round_index = card._round_index
        if round_index is None:
            logger.error("[DELETE] Card has no _round_index")
            return
        
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        
        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning(f"[DELETE] Invalid round_index: {round_index}")
            return
        
        success, old_count, new_count = truncate_and_remove_round(
            session, round_index, round_ranges
        )
        if not success:
            return
        
        log_deletion_stats(round_index, len(widgets_to_remove), old_count, new_count)
        
        # === 3. 同步 _message_batch ===
        self._message_batch = group_messages_for_display(session.messages)
        
        # === 4. 保存 session ===
        if self._current_session_id != session.session_id:
            self._current_session_id = session.session_id
        
        try:
            self._persist_session_after_mutation()
        except Exception as e:
            logger.error(f"[DELETE] Failed to persist session: {e}")
        
        self._finalize_local_session_mutation()

    def _undo_from_message(self, card: MessageCard):
        if card.role != "user":
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 关键修复：从 session 数据计算 round_index，不依赖 UI 布局
        # 这样即使卡片动态加载（只渲染部分），也能正确工作
        user_text = card.get_plain_text()
        timestamp = card.timestamp

        # 在 session.messages 中找到对应的 user 消息
        round_index = self._find_user_round_index_from_session(
            session, user_text, timestamp
        )
        if round_index is None:
            logger.warning("[UNDO] Cannot find user message in session")
            return

        if self._is_streaming:
            self._on_stop_clicked()

        # 获取待回滚的文件操作（从该轮次到最后的全部）
        all_call_ids = self._get_all_tool_call_ids_from_round(round_index)
        

        # 如果有文件操作，显示预览对话框
        if all_call_ids and self._tool_executor and self._tool_executor.file_recorder:
            # 使用辅助函数收集操作
            operations = collect_operations_for_round(
                self._tool_executor.file_recorder,
                self._current_session_id,
                all_call_ids
            )
            
            if operations:
                dialog = FileUndoPreviewDialog(operations, self)
                result = dialog.exec_()

                if result == FileUndoPreviewDialog.CANCEL:
                    return  # 取消撤销，什么都不做

                # 执行回滚 - 只还原选中的操作
                selected_ops = dialog.get_selected_operations()
                if selected_ops:
                    result = self._tool_executor.file_recorder.rollback_operations(selected_ops)
                    self._show_undo_result(result)

        user_input = card.get_plain_text()
        context_tags = card.context_tags.copy()
        if not self._truncate_session_from_user_round(round_index=round_index, card=card):
            return

        # 恢复输入框内容
        restore_input_from_card(self.input_area, card)

    def _get_last_tool_call_id_after_round(self, round_index: int) -> Optional[str]:
        """获取指定 round_index 之后最后一个 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return None

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        
        return find_last_tool_call_id_after_round(canonical_messages, round_ranges, round_index)

    def _get_all_tool_call_ids_from_round(self, round_index: int) -> List[str]:
        """获取从指定 round 到最后的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, _ = get_round_message_indices(session, round_index)
        if start_idx is None:
            return []

        canonical_messages = consolidate_messages(session.messages)
        
        # 使用辅助函数收集剩余的 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, len(canonical_messages))

    def _get_tool_call_ids_in_round(self, round_index: int) -> List[str]:
        """获取指定 round 范围内的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, end_idx = get_round_message_indices(session, round_index)
        if start_idx is None:
            return []

        canonical_messages = consolidate_messages(session.messages)
        
        # 使用辅助函数收集 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, end_idx)

    def _show_undo_result(self, result):
        """显示撤销结果"""
        if result.failed_count > 0:
            failed_list = format_file_list(result.failed_files, max_count=5)
            InfoBar.warning(
                "部分文件回滚失败",
                f"成功: {result.success_count}, 失败: {result.failed_count}\n{failed_list}",
                parent=self,
                duration=5000,
            )
        elif result.success_count > 0:
            InfoBar.success(
                "文件已回滚",
                f"已恢复 {result.success_count} 个文件",
                parent=self,
                duration=3000,
            )

    def _on_tool_diff_requested(self, tool_call_id: str):
        """
        处理工具差异对比请求
        
        Args:
            tool_call_id: 工具调用 ID
        """
        if not tool_call_id:
            return
        
        session = self.session_manager.get_current_session()
        if not session:
            return
        
        session_id = session.session_id
        
        # 检查是否有 file_recorder
        if not self._tool_executor or not self._tool_executor.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化")
            return
        
        try:
            # 获取该 tool_call_id 对应的文件操作记录
            operations = self._tool_executor.file_recorder.get_operations_for_preview(
                session_id=session_id,
                call_id=tool_call_id
            )
            
            # 使用辅助函数获取第一个文件操作
            success, backup_path, _ = get_first_file_operation(operations)
            if not success:
                InfoBar.warning(
                    "无差异信息",
                    "此工具没有修改任何文件，或备份信息已丢失",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 使用辅助函数读取备份文件和生成 diff
            old_content, new_content, backup_file = read_backup_files(backup_path)
            html = generate_diff_html(old_content, new_content, backup_file)
            
            # 显示差异
            show_diff_viewer(self, html)
            
        except Exception as e:
            logger.error(f"[LLMChatter] 显示工具差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _on_subagent_log_requested(self, task_ids_str: str):
        """
        处理子智能体日志查看请求
        
        Args:
            task_ids_str: 逗号分隔的任务ID列表
        """
        if not task_ids_str:
            return
        
        # 解析 task_ids
        task_ids = [tid.strip() for tid in task_ids_str.split(",") if tid.strip()]
        if not task_ids:
            return
        
        # 获取 sub_agent_manager
        sub_agent_mgr = self._tool_executor._builtin_tools._sub_agent_manager
        if not sub_agent_mgr:
            logger.warning("[LLMChatter] sub_agent_manager 未初始化")
            return
        
        # 如果只有一个任务，直接显示
        if len(task_ids) == 1:
            task_id = task_ids[0]
            task_data = sub_agent_mgr.get_task_logs(task_id)
            if not task_data.get("found"):
                InfoBar.warning(
                    "任务不存在",
                    f"未找到任务: {task_id[:8]}...",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return
            
            # 显示日志
            self._sub_agent_floating_widget.show_task_from_data(task_data)
            return
        
        # 多个任务：收集所有任务的日志
        all_logs = []
        for task_id in task_ids:
            task_data = sub_agent_mgr.get_task_logs(task_id)
            if task_data.get("found"):
                all_logs.append(task_data)
        
        if not all_logs:
            InfoBar.warning(
                "任务不存在",
                "未找到任何任务日志",
                duration=3000,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )
            return
        
        # 清空现有面板并逐个添加任务
        for i, task_data in enumerate(all_logs):
            task_id = task_data.get("task_id", task_data.get("summary", {}).get("task_id", "unknown"))
            # 首次清空，后续追加
            self._sub_agent_floating_widget.show_task_from_data(task_data, clear_first=(i == 0))
        
        # 显示面板
        self._sub_agent_floating_widget.setVisible(True)

    def _on_card_diff_requested(self, round_index: int):
        """
        处理卡片级差异对比请求，汇总一次对话中所有工具调用的文件修改。

        Args:
            round_index: 用户回合索引
        """
        if round_index is None:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        session_id = session.session_id

        # 检查是否有 file_recorder
        if not self._tool_executor or not self._tool_executor.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化")
            return

        try:
            # 获取该 round 范围内的所有 tool_call_id
            all_call_ids = self._get_tool_call_ids_in_round(round_index)

            if not all_call_ids:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 使用辅助函数收集所有工具的文件操作
            all_operations = collect_operations_for_round(
                self._tool_executor.file_recorder,
                session_id,
                all_call_ids
            )

            if not all_operations:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件，或备份信息已丢失",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 使用辅助函数生成合并的 diff HTML
            html = generate_multi_file_diff_html(all_operations)
            
            # 显示差异
            show_diff_viewer(self, html)

        except Exception as e:
            logger.error(f"[LLMChatter] 显示卡片差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _on_save_file_requested(self, code: str, lang: str):
        """
        处理保存文件请求

        Args:
            code: 代码内容
            lang: 代码语言
        """
        # 使用辅助函数获取默认文件名和扩展名
        ext = get_language_extension(lang)
        default_name = get_default_save_filename(lang, code)

        # 弹出文件保存对话框
        from PyQt5.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存代码文件",
            default_name,
            f"代码文件 (*{ext});;所有文件 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(code)
            InfoBar.success(
                "文件已保存",
                file_path,
                duration=3000,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )
        except Exception as e:
            logger.error(f"[LLMChatter] 保存文件失败: {e}")
            InfoBar.error(
                "保存失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _on_code_action(self, code: str, action: str = "copy"):
        if action == "insert":
            self.insertResponse.emit(code)
        elif action == "create":
            self.createResponse.emit(code)
        elif action == "copy":
            clipboard = QApplication.clipboard()
            clipboard.setText(code)
            InfoBar.success(
                "已复制",
                "",
                duration=1500,
                parent=self.homepage,
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _scroll_to_bottom(self, sticky_ms: int = 0):
        self._pending_scroll_to_bottom = True
        if sticky_ms > 0:
            self._bottom_anchor_deadline = max(
                self._bottom_anchor_deadline,
                time.monotonic() + sticky_ms / 1000.0,
            )
        self._scroll_bottom_timer.start()

    def _do_scroll_to_bottom(self):
        if not self._pending_scroll_to_bottom:
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        max_val = scroll_bar.maximum()
        scroll_bar.setValue(max_val)
        # 再次设置确保卡片高度变化后仍在底部
        scroll_bar.setValue(max_val)
        self._pending_scroll_to_bottom = False
        if self._bottom_anchor_deadline > time.monotonic():
            self._bottom_anchor_timer.start()
        # 加载完成后抑制滚动同步，避免节点跑到渲染的卡片数量位置
        self._suppress_scroll_sync_count = 0

    def _maintain_bottom_anchor(self):
        if self._bottom_anchor_deadline <= time.monotonic():
            self._bottom_anchor_deadline = 0.0
            self._suppress_scroll_sync_count = 0
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self._bottom_anchor_timer.start()

    def _on_message_card_height_changed(self, _height: int):
        if self._bottom_anchor_deadline > time.monotonic():
            self._pending_scroll_to_bottom = True
            self._scroll_bottom_timer.start()

    def handle_recommended_question(self, content: str, action: str):
        if action == "ask":
            self.input_area.clear()
            self.send_preset_question(content)
        elif action == "session":
            # session_id 直接就是 content
            session_id = content.strip()
            self._switch_to_session_by_id(session_id)

    def _switch_to_session_by_id(self, session_id: str):
        """根据 session_id 切换到对应会话"""
        if not session_id:
            return
        
        # 先在当前 session_manager 中查找
        for i, session in enumerate(self.session_manager.get_all_sessions()):
            if session.session_id == session_id:
                self.session_manager.switch_to_session(i)
                self._display_current_session()
                self._hide_welcome_cards()
                return
        
        # 再从 history_manager 查找并恢复（通过 session_id 直接获取）
        session_record = self.history_manager.get_session_by_session_id(session_id)
        if session_record:
            messages = self.history_manager.get_session_messages(session_id)
            title = session_record.get("title") or session_record.get("name") or "历史对话"
            from app.widgets.ui_helpers import create_session_from_record, init_after_loading_session
            restored = create_session_from_record(session_record, messages, title)
            init_after_loading_session(self, restored, session_id, title, self._tool_executor)
            # 同步项目
            session_project = session_record.get("project", "默认项目") or "默认项目"
            self._current_project = session_project
            self._project_label.setText(session_project)
            self._display_current_session()
            self._hide_welcome_cards()
        else:
            logger.warning(f"未找到 session_id: {session_id}")

    def send_preset_question(self, question: str):
        if not isinstance(question, str) or not question.strip():
            return
        self._on_send_clicked(user_text=question.strip())

    def _on_send_clicked(self, user_text: str = ""):
        if self._is_streaming:
            self._on_stop_clicked()

        if not user_text:
            user_text = self.input_area.toPlainText().strip()

        if not user_text:
            return

        # 检查模型配置
        llm_config = self._get_current_model_config()
        if not llm_config or not llm_config.get("API_KEY"):
            InfoBar.warning(
                "请先选择模型",
                "请在设置中选择一个可用的模型后再发送消息",
                parent=self,
                duration=3000,
            )
            return

        self._hide_welcome_cards()

        context_params = {}

        self.input_area.clear()
        self._append_user_message(user_text, tag_params=context_params)

        assistant_card = self._append_assistant_message()

        self._is_streaming = True
        self._toggle_send_stop(True)

        # 关键修复：确保 ToolExecutor 使用正确的 session_id
        session = self.session_manager.get_current_session()
        if session and self._tool_executor:
            self._tool_executor.set_session_context(session.session_id)

        # 如果 send_message 返回 False（通常是 LLM 配置无效），回滚 UI 状态
        if not self._chat_engine.send_message(user_text, context_params):
            self._is_streaming = False
            self._toggle_send_stop(False)
            assistant_card.deleteLater()
            return

        self._current_assistant_card = assistant_card
        self._maybe_generate_topic_summary()

    def _on_stream_started(self):
        self._is_streaming = True
        self._accumulated_content = ""

    def _on_content_received(self, content_piece: str):
        if self._current_assistant_card:
            self._update_assistant_message(self._current_assistant_card, content_piece)

        if not hasattr(self, "_accumulated_content"):
            self._accumulated_content = ""
        self._accumulated_content += content_piece

    def _on_reasoning_content_received(self, reasoning_piece: str):
        """处理 DeepSeek 思考内容（流式接收）"""
        if self._current_assistant_card:
            self._current_assistant_card.append_reasoning(reasoning_piece)

    def _on_tool_call_started(
        self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None
    ):
        import time

        self._current_tool_start_time = time.time()
        self._current_tool_call_id = tool_call_id
        self._current_tool_name = tool_name
        self._current_tool_args = arguments

        if tool_name == "question":
            question_text = arguments.get("question", "")
            options = arguments.get("options", [])
            multiple = arguments.get("multiple", False)
            if question_text:
                self._question_tool_call_id = tool_call_id
                if not isinstance(options, list):
                    options = []
                self._question_floating_widget.show_question(
                    question_text, options, multiple
                )
            return

        if tool_name in ("todowrite", "todoread"):
            self._todo_floating_widget.setVisible(True)
            return

        self._tool_floating_widget.start_tool(tool_name, arguments)

    def _on_sub_agent_task_started(self, task_id: str, agent_name: str, task_description: str):
        """子智能体任务启动（通过 SubAgentManager 信号触发）"""
        widget = self._sub_agent_floating_widget
        
        # 新批次开始时清空面板（_batch_started 为 False 表示新批次）
        if not widget._batch_started:
            widget.clear()
            widget.setVisible(True)
        
        widget._batch_started = True  # 标记批次已开始
        widget.add_task(task_id, agent_name, task_description)

        # 连接 executor 信号
        sub_agent_mgr = self._tool_executor._builtin_tools._sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)
        if executor:
            executor.progress_updated.connect(lambda tid, msg: self._sub_agent_floating_widget.update_progress(tid, msg))
            executor.tool_call_started.connect(lambda tid, name, args: self._sub_agent_floating_widget.add_tool_call(tid, name, args))
            executor.tool_result_received.connect(lambda tid, name, result, success: self._sub_agent_floating_widget.add_tool_result(tid, name, result, success))
            executor.finished_with_result.connect(lambda tid, result: self._on_sub_agent_finished(tid, result))

    def _on_sub_agent_task_finished(self, task_id: str, result: str):
        """子智能体任务完成"""
        # 从管理器获取执行器，检查是否有真实的错误状态
        sub_agent_mgr = self._tool_executor._builtin_tools._sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)
        
        # 优先使用 executor 中记录的 _execution_error 来判断成功/失败
        # 而不是依赖结果内容中的关键词（这会导致误判）
        execution_error = getattr(executor, "_execution_error", None) if executor else None
        success = execution_error is None or execution_error == ""
        
        self._sub_agent_floating_widget.finish_task(task_id, result, success)

        # 从管理器移除并记录结果
        if executor:
            agent_name = getattr(executor, "agent_name", "")
            del sub_agent_mgr._running_tasks[task_id]
        else:
            agent_name = ""
        sub_agent_mgr._finished_tasks[task_id] = {"result": result, "error": execution_error or "", "agent_name": agent_name}

    def _on_sub_agent_finished(self, task_id: str, result: str):
        """单个子智能体执行完成"""
        self._sub_agent_manager.task_finished.emit(task_id, result)

    def _on_tool_cancelled(self):
        """工具执行被用户中止"""
        logger.info("[ToolFloatingWidget] Tool execution cancelled by user")

        self._tool_cancelled_by_user = True
        self._cancelled_tool_call_id = getattr(self, "_current_tool_call_id", None)
        self._tool_floating_widget.finish_tool("用户中止", success=False)

        tool_call_id = getattr(self, "_current_tool_call_id", None)
        tool_name = getattr(self, "_current_tool_name", "unknown")
        tool_args = getattr(self, "_current_tool_args", {})

        if tool_call_id and self._current_assistant_card:
            self._current_assistant_card.append_tool_result(
                tool_name=tool_name,
                arguments=tool_args,
                result="[工具执行已被用户中止]",
                success=False,
                tool_call_id=tool_call_id,
            )
            self._scroll_to_bottom()

    def _on_tool_result_received(
        self, tool_call_id: str, tool_name: str, arguments: dict, result: Any
    ):
        import time

        if (
            self._tool_cancelled_by_user
            and tool_call_id == self._cancelled_tool_call_id
        ):
            # 支持 dict 和 ToolResult 两种格式
            if isinstance(result, dict):
                error_msg = result.get("error", "") or ""
            else:
                error_msg = str(getattr(result, "error", "") or "")
            if "用户中止" in error_msg:
                self._tool_floating_widget.finish_tool("用户中止", success=False)
                return
            return

        elapsed = (
            time.time() - self._current_tool_start_time
            if hasattr(self, "_current_tool_start_time")
            else 0
        )

        # 支持 ToolResult 对象和 dict 格式的 result
        if isinstance(result, dict):
            success = result.get("success", True)
            error_msg = result.get("error", "") or ""
            content = error_msg if not success else (str(result.get("content", "")) if result.get("content") is not None else "")
        else:
            success = getattr(result, "success", True) if hasattr(result, "success") else True
            error_msg = str(getattr(result, "error", "") or "")
            content = str(result) if result else ""

        if tool_name not in ("question", "task", "todowrite", "todoread"):
            self._tool_floating_widget.show_if_needed(elapsed)
            self._tool_floating_widget.finish_tool(content[:200], success)

        if tool_name in ("todowrite", "todoread"):
            todos = self._tool_executor.todo_list if self._tool_executor else []
            self._todo_floating_widget.update_todos(todos)
            self._todo_floating_widget.setVisible(True)

        if self._current_assistant_card:
            self._current_assistant_card.append_tool_result(
                tool_name=tool_name,
                arguments=arguments or {},
                result=content,
                success=success,
                tool_call_id=tool_call_id,
            )

        self._scroll_to_bottom()

    def _find_latest_assistant_card(self) -> Optional[MessageCard]:
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard) and widget.role == "assistant":
                    return widget
        return None

    def _notify_if_inactive(self, title: str, message: str):
        setting = Settings.get_instance()
        if not setting.llm_notify_enabled.value:
            return

        if not self._should_show_inactive_notification():
            return

        sound_type = setting.llm_notify_sound.value
        if sound_type != "none":
            QApplication.beep()

        # 从当前窗口的顶层窗口获取 tray_icon（self.window() 返回包含此 widget 的顶层窗口）
        win = self.window()
        if win and hasattr(win, "tray_icon") and win.tray_icon:
            if win.tray_icon.isVisible():
                win.tray_icon.showMessage(
                    title, message, win.tray_icon.MessageIcon(1), 4000
                )

    def _should_show_inactive_notification(self) -> bool:
        """Only notify when the app window is not effectively visible to the user."""
        window = None
        if self.homepage and self.homepage.window():
            window = self.homepage.window()
        else:
            window = self.window()

        if window is None:
            return True

        if not window.isVisible() or window.isMinimized():
            return True

        native_result = self._is_window_in_foreground_native(window)
        if native_result is not None:
            return not native_result

        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None

        # When the current app is not foreground, the chat reply should notify.
        if active_window is None:
            return True

        if active_window is window:
            return False

        return not window.isActiveWindow()

    def _is_window_in_foreground_native(self, window) -> Optional[bool]:
        """Use the OS foreground window when available to avoid Qt focus misreads."""
        if os.name != "nt":
            return None

        try:
            user32 = ctypes.windll.user32
            foreground_hwnd = user32.GetForegroundWindow()
            if not foreground_hwnd:
                return None

            foreground_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(
                foreground_hwnd, ctypes.byref(foreground_pid)
            )
            return foreground_pid.value == os.getpid()
        except Exception:
            return None

    def _on_notification_clicked(self):
        window = self.window()
        if window:
            window.show()
            if window.isMinimized():
                window.showNormal()
            window.activateWindow()

    def _on_stream_finished(self, response: str):
        self._is_streaming = False
        self._tool_cancelled_by_user = False
        self._cancelled_tool_call_id = None
        self._toggle_send_stop(False)

        if self._current_assistant_card:
            self._current_assistant_card.finish_streaming()
        if self.history_manager:
            self._save_current_session_to_history()

        if self.input_area:
            self.input_area.setFocus()

        session = self.session_manager.get_current_session()
        if session and session.messages:
            last_msg = session.messages[-1] if session.messages else None
            if last_msg and last_msg.get("role") == "assistant":
                content = last_msg.get("content", "")
                if isinstance(content, list):
                    from app.utils.message_content import (
                        content_to_text,
                    )

                    content = content_to_text(content)
                preview = content[:50] + "..." if len(content) > 50 else content
                current_title = self.title_edit.text() if self.title_edit else "对话完成"
                self._notify_if_inactive(current_title, preview)

        # 对话完成后刷新余额显示
        self._refresh_balance()

    def _refresh_balance(self):
        """刷新余额显示（对话完成后调用）"""
        print(f"[Balance] _refresh_balance called, provider={getattr(self, '_current_provider_name', 'None')}")
        balance_display = getattr(self, "balance_display", None)
        if balance_display:
            # 如果当前服务商支持余额查询，则刷新
            provider_name = getattr(self, "_current_provider_name", "")
            print(f"[Balance] provider_name={provider_name}")
            if provider_name in ("DeepSeek", "SiliconFlow (硅基流动)"):
                config = self._valid_configs.get(provider_name, {})
                api_key = config.get("API_KEY", "")
                print(f"[Balance] api_key exists: {bool(api_key)}")
                if api_key:
                    balance_display.set_provider(provider_name, api_key)
                    return
            # 如果不支持余额查询，隐藏
            balance_display.setVisible(False)

    def _save_current_session_to_history(self):
        session = self.session_manager.get_current_session()
        saved_messages = list(session.messages or []) if session else []
        if not saved_messages:
            return

        system_prompt = getattr(session, "system_prompt", "") or ""
        # 优先使用已有的 topic_summary，避免被用户消息前30字覆盖
        session_title = getattr(session, "topic_summary", "") or ""

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session(
                    idx,
                    saved_messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=self._current_project,
                )
            else:
                self.history_manager.save_session(
                    saved_messages,
                    title=session_title,  # 使用已有的 topic_summary
                    session_id=session.session_id if session else None,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=self._current_project,
                )
                self._current_session_id = session.session_id if session else None
        else:
            self.history_manager.save_session(
                saved_messages,
                title=session_title,  # 使用已有的 topic_summary
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
                project=self._current_project,
            )
            self._current_session_id = session.session_id if session else None

        self._update_node_preview()

    def _on_messages_updated(self, messages: List[Dict[str, Any]]):
        session = self.session_manager.get_current_session()
        if not session:
            return

        self._history_preview_messages = None
        session.set_messages(messages or [], preserve_compaction=True)
        self._refresh_context_usage_indicator()

    def _on_engine_error(self, error: str):
        self._tool_cancelled_by_user = False

        if self._current_assistant_card:
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.set_error_state(True)
            self._current_assistant_card.update_content(error)

        self._is_streaming = False

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        self._toggle_send_stop(False)

        current_title = self.title_edit.text() if self.title_edit else "对话"
        self._notify_if_inactive(f"{current_title} - 错误", error[:100])

    def _on_user_message_added(self, user_text: str):
        pass

    def _on_skill_requested(self, method: str, params: dict):
        result = self._tool_executor.execute_skill(method, params)
        content = (
            f"[Skill Result] {result}"
            if "error" not in result
            else f"[Skill Error] {result.get('error')}"
        )
        new_card = self._append_assistant_message()
        new_card.update_content(str(content))
        new_card.finish_streaming()
        self._scroll_to_bottom()

    def _on_question_asked(
        self, tool_call_id: str, question: str, options: list, multiple: bool = False
    ):
        self._question_tool_call_id = tool_call_id
        if not isinstance(options, list):
            options = []
        self._question_floating_widget.show_question(question, options, multiple)
        self._notify_if_inactive("需要回答问题", question[:100])

    def _on_question_answered(self, answer: str):
        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            if answer == "允许":
                self._chat_engine.approve_tool_permission(tool_call_id, False)
            elif answer == "允许且该轮对话自动允许":
                self._chat_engine.approve_tool_permission(tool_call_id, True)
            else:
                self._chat_engine.deny_tool_permission(tool_call_id)
            if self.input_area:
                self.input_area.setFocus()
            return

        if not self._question_tool_call_id:
            return

        tool_call_id = self._question_tool_call_id
        self._question_tool_call_id = None

        if self._chat_engine:
            self._chat_engine.provide_question_answer(answer)

        if self.input_area:
            self.input_area.setFocus()

    def _on_question_cancelled(self):
        """用户关闭问题窗口时，返回空答案让大模型继续"""
        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            self._chat_engine.deny_tool_permission(tool_call_id)
            if self.input_area:
                self.input_area.setFocus()
            return

        if not self._question_tool_call_id:
            return

        self._question_tool_call_id = None

        if self._chat_engine:
            self._chat_engine.provide_question_answer("")

        if self.input_area:
            self.input_area.setFocus()

    def _on_agent_switched(self, agent_name: str):
        """智能体切换回调 - 丝滑切换，不清空对话"""
        pass

    def _on_permission_approval_requested(
        self, tool_call_id: str, tool_name: str, arguments: dict
    ):
        self._pending_permission_tool_call_id = tool_call_id
        self._pending_permission_auto_allow = False
        try:
            arg_str = str(arguments)[:200] if arguments else ""
            question_text = f"工具 `{tool_name}` 需要权限执行。\n\n参数: {arg_str}"
            options = ["允许", "允许且该轮对话自动允许", "不允许"]
            self._question_floating_widget.show_question(question_text, options, False)
        except Exception as e:
            logger.error(f"[Permission] Approval error: {e}")
            self._chat_engine.deny_tool_permission(tool_call_id)
            self._pending_permission_tool_call_id = None

    def _maybe_generate_topic_summary(self):
        selected_name = self._current_provider_name if self._current_provider_name else "系统默认配置"
        llm_config = self._valid_configs.get(selected_name)
        if not llm_config:
            logger.warning("[Topic Summary] No LLM config found, skipping")
            return
        session = self.session_manager.get_current_session()
        if not session:
            logger.warning("[Topic Summary] No session found, skipping")
            return

        user_messages = [m for m in session.messages if m.get("role") == "user"]
        if not user_messages:
            logger.warning("[Topic Summary] No user messages found, skipping")
            return
        previous_summary = ""
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                previous_summary = self.history_manager.get_topic_summary(idx)

        long_term_memory = (
            self._memory_manager.get_context_string() if self._memory_manager else ""
        )

        existing_memories = (
            self._memory_manager.get_user_memories() if self._memory_manager else []
        )

        task = TopicSummaryTask(
            messages=session.messages,
            llm_config=llm_config,
            callback=self._on_topic_summary_generated,
            previous_summary=previous_summary if previous_summary else None,
            long_term_memory=long_term_memory,
            existing_memories=existing_memories,
        )
        self._gen_thread_pool.start(task)

    def _on_topic_summary_generated(self, result, error: str = None):
        if error:
            logger.error(f"[Topic Summary] Failed to generate: {error}")
            return
        if not result:
            return

        if isinstance(result, dict):
            summary = result.get("topic_summary", "")
            should_update_memory = result.get("should_update_memory", False)
            memory_content = result.get("memory_content", "")
            memory_category = result.get("memory_category", "task_preference")
            hit_memories = result.get("hit_memories", [])
        else:
            summary = result
            should_update_memory = False
            memory_content = ""
            memory_category = "task_preference"
            hit_memories = []

        if result.get("title_unchanged") and self._current_session_id is not None:
            # 标题未更新，保持现有标题
            logger.info("[Topic Summary] 标题未更新，保持现有标题")
            return

        if not summary:
            return

        clean_summary = summary.strip()

        # 校验标题长度，超长说明解析异常或LLM输出异常，跳过更新
        MAX_TITLE_LENGTH = 50
        if len(clean_summary) > MAX_TITLE_LENGTH:
            logger.warning(f"[Topic Summary] 标题过长({len(clean_summary)}字)，跳过更新")
            return

        session = self.session_manager.get_current_session()

        # 先设置 session 的 topic_summary，避免 save_session 时 title 为空
        if session:
            session.set_topic_summary(clean_summary)

        if self._current_session_id is None and session and session.messages:
            self.history_manager.save_session(
                session.messages if session else [],
                title=clean_summary,  # 使用生成的摘要作为标题
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
            )
            self._current_session_id = session.session_id if session else None

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_topic_summary(idx, clean_summary)

        self._update_title_display(clean_summary)

        if should_update_memory and memory_content and self._memory_manager:
            self._memory_manager.add_user_memory(
                memory_content,
                source="topic_summary",
                confidence=0.8,
                category=memory_category,
            )
            logger.info(
                f"[Topic Summary] Added to long-term memory [{memory_category}]: {memory_content[:50]}..."
            )
        else:
            logger.info(
                f"[Topic Summary] Memory update skipped (should_update={should_update_memory}, content={bool(memory_content)})"
            )

        if hit_memories and self._memory_manager:
            self._memory_manager.touch_memories(hit_memories)
            logger.info(
                f"[Topic Summary] Touched {len(hit_memories)} existing memories"
            )

    def _update_title_display(self, title: str):
        self.title_edit.setText(title)

    def _update_project_display(self, project: str):
        """更新项目名称显示"""
        self._current_project = project
        self._project_label.setText(project)

    def _on_project_label_clicked(self, event):
        """项目标签点击 - 显示项目选择 popup"""
        self._show_project_selector_popup()

    def _show_project_selector_popup(self):
        """显示项目选择弹窗"""
        from app.widgets.project_selector_popup import ProjectSelectorPopup
        if hasattr(self, '_project_selector_popup') and self._project_selector_popup:
            self._project_selector_popup.close()
            self._project_selector_popup.deleteLater()

        projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
        self._project_selector_popup = ProjectSelectorPopup(
            projects=projects,
            current_project=self._current_project,
            parent=self
        )
        self._project_selector_popup.projectSelected.connect(self._on_project_selected)
        self._project_selector_popup.newProjectCreated.connect(self._on_new_project_created)
        self._project_selector_popup.show_at(self._project_label)

    def _on_project_selected(self, project: str):
        """切换到选中的项目"""
        self._current_project = project
        self._project_label.setText(project)
        # 保存到配置
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        cfg.current_project.value = project
        cfg.save()
        # 刷新历史面板（切换项目过滤）
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()
        # 自动触发新建会话，避免原会话与切换后的项目不匹配
        self._create_new_session()

    def _on_new_project_created(self, project: str):
        """新建项目后"""
        self._current_project = project
        self._project_label.setText(project)
        # 保存到配置
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        cfg.current_project.value = project
        cfg.save()
        # 刷新历史面板
        self._history_popup_card.refreshRequested.emit()
        # 自动触发新建会话
        self._create_new_session()

    def _show_soul_memory(self):
        if not self._memory_manager:
            return
        user_memories = self._memory_manager.get_user_memories()

        dialog = MemoryManagerDialog(user_memories, self)
        dialog.memoryUpdated.connect(self._on_memory_updated)
        dialog.exec_()

    def _on_memory_updated(self, memories: list):
        if not self._memory_manager:
            return
        self._memory_manager.update_user_memories(memories)
        InfoBar.success("已保存", "长期记忆已更新", parent=self, duration=1500)

    def _on_title_double_click(self, event):
        from PyQt5.QtWidgets import QInputDialog, QLineEdit

        current_title = self.title_edit.text()
        new_title, ok = QInputDialog.getText(
            self, "编辑标题", "请输入新标题:", QLineEdit.Normal, current_title
        )
        if ok and new_title.strip():
            self._update_title(new_title.strip())

    def _update_title(self, new_title: str):
        self.title_edit.setText(new_title)
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session_title(idx, new_title)

    def _auto_save_current_session(self):
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            return

        system_prompt = getattr(session, "system_prompt", "") or ""

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session(
                    idx,
                    session.messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                )
            else:
                self.history_manager.save_session(
                    session.messages,
                    session_id=session.session_id,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                )
                self._current_session_id = session.session_id
        else:
            self.history_manager.save_session(
                session.messages,
                session_id=session.session_id,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
            )
            self._current_session_id = session.session_id

        if self._current_session_id:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                return self.history_manager.get_current_title(idx)
        return None

    def closeEvent(self, event):
        try:
            self._auto_save_current_session()
        except Exception:
            pass
        super().closeEvent(event)

    def _toggle_send_stop(self, is_sending: bool):
        if is_sending:
            self.history_btn.setDisabled(True)
            self.input_area.toggle_send_button(False)
        else:
            self.history_btn.setDisabled(False)
            self.input_area.toggle_send_button(True)

    def _on_stop_clicked(self):
        self._tool_cancelled_by_user = False
        interrupted_messages: List[Dict[str, Any]] = []

        if self._chat_engine:
            interrupted_messages = self._chat_engine.stop() or []

        self._is_streaming = False

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        self._toggle_send_stop(False)
        if self._current_assistant_card:
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.finish_streaming()

        if interrupted_messages:
            self._on_messages_updated(interrupted_messages)
            if self.history_manager:
                self._save_current_session_to_history()
        InfoBar.warning(
            title="已中止",
            content="问答请求已被手动中止。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
            parent=self,
        )
        if self.input_area:
            self.input_area.setFocus()

    def _create_context_menu(self):
        self._context_menu_actions = {}
        self.menu_btn.clicked.connect(self._show_context_menu)

    def _show_context_menu(self):
        from PyQt5.QtWidgets import QMenu

        menu = QMenu(self)
        export_action = menu.addAction("导出对话记录")
        export_action.triggered.connect(self._export_conversation)
        clear_action = menu.addAction("清空当前对话")
        clear_action.triggered.connect(self._clear_current_conversation)
        menu.exec_(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomRight()))

    def _export_conversation(self):
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            InfoBar.warning("无法导出", "当前没有对话内容", parent=self)
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出对话",
            f"对话_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
            "Markdown Files (*.md);;Text Files (*.txt)",
        )
        if not file_path:
            return
        try:
            # 使用辅助函数导出对话
            content = export_messages_to_markdown(session.messages)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            InfoBar.success("导出成功", f"已保存到: {file_path}", parent=self)
        except Exception as e:
            InfoBar.error("导出失败", str(e), parent=self)

    def _clear_current_conversation(self):
        self._create_new_session()
        InfoBar.success("已清空", "开始新的对话", parent=self, duration=1500)
