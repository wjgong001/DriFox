# -*- coding: utf-8 -*-
"""
任务队列卡片 - 看板式布局
显示不同状态的四个列：未触发 | 队列中 | 执行中 | 已完成
"""

import os
from typing import Dict, Any, List
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QScrollArea, QFrame, QPushButton, QSizePolicy
)
from PyQt5.QtGui import QFont

from app.utils.utils import get_unified_font, get_icon
from app.core.task_watcher import TaskConfig


class TaskQueueCard(QFrame):
    """任务队列卡片 - 看板式布局"""
    
    task_triggered = pyqtSignal(str, TaskConfig)  # 触发任务
    refresh_requested = pyqtSignal()
    
    def __init__(self, task_system=None, parent=None):
        super().__init__(parent)
        
        self._task_system = task_system
        self._cached_tasks: Dict[str, Any] = {}
        self._column_data = {
            "pending": [],     # 待入队（文件中的任务）
            "queued": [],      # 队列中
            "running": [],     # 执行中
            "completed": [],   # 已完成
        }
        
        self._setup_ui()
    
    def set_task_system(self, system):
        """设置任务系统"""
        # 只接受 TaskWatcherSystem 实例
        if system is None:
            self._task_system = None
            return
        
        # 检查是否是有效的 TaskWatcherSystem（必须有这些属性）
        is_valid = (
            hasattr(system, 'enqueue_task') and 
            hasattr(system, '_queue') and 
            hasattr(system, '_task_engine') and
            hasattr(system, 'get_queue_stats')
        )
        
        if is_valid:
            self._task_system = system
        else:
            from loguru import logger
            logger.warning(f"[TaskQueueCard] 忽略无效的 task_system: {type(system)}")
            self._task_system = None
    
    def refresh(self):
        self._load_tasks()
    
    def _setup_ui(self):
        self.setFixedHeight(320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("""
            TaskQueueCard {
                background-color: rgba(33, 33, 38, 250);
                border: 1px solid #3d3d3d;
                border-radius: 8px;
            }
        """)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(8)
        
        # 标题栏
        header = QHBoxLayout()
        title_label = QLabel("📋 任务管理", self)
        title_label.setFont(get_unified_font(11, True))
        title_label.setStyleSheet("color: #f59e0b;")
        
        refresh_btn = QPushButton("🔄", self)
        refresh_btn.setFont(get_unified_font(10))
        refresh_btn.setFixedSize(28, 24)
        refresh_btn.setToolTip("刷新")
        refresh_btn.clicked.connect(self.refresh)
        
        close_btn = QPushButton("✕", self)
        close_btn.setFont(get_unified_font(10))
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("color: #888888; background: transparent; border: none;")
        close_btn.clicked.connect(lambda: self.hide())
        
        header.addWidget(title_label)
        header.addStretch()
        header.addWidget(refresh_btn)
        header.addWidget(close_btn)
        main_layout.addLayout(header)
        
        # 看板区域
        board_layout = QHBoxLayout()
        board_layout.setSpacing(8)
        
        # 四个列
        self._columns = {
            "pending": self._create_column("📁 未触发", "#6b7280"),
            "queued": self._create_column("⏳ 队列中", "#eab308"),
            "running": self._create_column("🔄 执行中", "#22c55e"),
            "completed": self._create_column("✅ 已完成", "#3b82f6"),
        }
        
        for col_widget in self._columns.values():
            board_layout.addWidget(col_widget, 1)
        
        main_layout.addLayout(board_layout)
        
        # 加载任务数据
        self._load_tasks()
    
    def _create_column(self, title: str, color: str) -> QFrame:
        """创建看板列"""
        col = QFrame(self)
        col.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(45, 45, 50, 180);
                border: 1px solid rgba(100, 100, 100, 80);
                border-radius: 6px;
                padding: 4px;
            }}
        """)
        
        layout = QVBoxLayout(col)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        
        # 列标题
        title_label = QLabel(title, col)
        title_label.setFont(get_unified_font(9, True))
        title_label.setStyleSheet(f"color: {color};")
        
        # 计数标签
        count_label = QLabel("0", col)
        count_label.setFont(get_unified_font(9))
        count_label.setStyleSheet("color: #666666;")
        count_label.setObjectName("count")
        
        title_layout = QHBoxLayout()
        title_layout.setSpacing(4)
        title_layout.addWidget(title_label)
        title_layout.addWidget(count_label)
        title_layout.addStretch()
        
        layout.addLayout(title_layout)
        
        # 任务列表区域
        list_widget = QWidget(col)
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 4, 0, 4)
        list_layout.setSpacing(4)
        list_layout.addStretch()
        
        scroll = QScrollArea(col)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollArea > QWidget > QScrollBar { background: transparent; }
        """)
        scroll.setWidget(list_widget)
        
        layout.addWidget(scroll, 1)
        
        col._title_layout = title_layout
        col._count_label = count_label
        col._list_layout = list_layout
        col._list_widget = list_widget
        
        return col
    
    def _load_tasks(self):
        """加载所有任务"""
        # 清空列数据
        for key in self._column_data:
            self._column_data[key] = []
        
        # 1. 加载文件系统中的任务（未触发）
        self._load_pending_tasks()
        
        # 2. 加载队列中的任务
        self._load_queued_tasks()
        
        # 3. 更新 UI
        self._update_columns()
    
    def _load_pending_tasks(self):
        """加载文件系统中的任务"""
        tasks_dir = os.path.join(".drifox", "tasks")
        if not os.path.exists(tasks_dir):
            return
        
        parser = None
        for file in os.listdir(tasks_dir):
            if not file.endswith(".task.md"):
                continue
            
            file_path = os.path.join(tasks_dir, file)
            config = self._get_cached_config(file_path)
            if config:
                self._column_data["pending"].append({
                    "config": config,
                    "source": "file",
                    "file_path": file_path,
                })
    
    def _load_queued_tasks(self):
        """加载队列任务"""
        if not self._task_system:
            return
        
        try:
            # 获取队列统计
            stats = self._task_system.get_queue_stats()
            pending_count = stats.get("pending", 0)
            running_count = stats.get("running", 0)
            
            # 队列中的任务
            if pending_count > 0:
                queue_items = self._get_queue_items()
                for item in queue_items[:5]:  # 最多显示 5 个
                    config = item.get("config")
                    if config:
                        self._column_data["queued"].append({
                            "config": config,
                            "source": "queue",
                            "item": item,
                        })
            
            # 执行中的任务
            if running_count > 0:
                for task_id, _ in self._task_system._pending_tasks.items():
                    config = self._task_system.get_task(task_id)
                    if config:
                        self._column_data["running"].append({
                            "config": config,
                            "source": "running",
                        })
            
            # 已完成的任务
            self._load_completed_tasks()
            
        except Exception as e:
            from loguru import logger
            logger.error(f"[TaskQueueCard] 加载队列任务失败: {e}")
    
    def _load_completed_tasks(self):
        """加载已完成任务"""
        # 从数据库获取最近完成的任务
        if self._task_system and hasattr(self._task_system, '_db'):
            try:
                rows = self._task_system._db.fetch_all(
                    "SELECT * FROM task_queue WHERE status IN ('completed', 'failed') ORDER BY completed_at DESC LIMIT 10"
                )
                for row in rows:
                    task_id = row["task_id"]
                    config = self._task_system.get_task(task_id)
                    if config:
                        self._column_data["completed"].append({
                            "config": config,
                            "source": "completed",
                            "status": row["status"],
                        })
            except Exception:
                pass
    
    def _get_cached_config(self, file_path: str):
        """获取缓存的任务配置"""
        from app.core.task_watcher import TaskParser
        parser = TaskParser()
        
        return parser.parse_file(file_path)
    
    def _get_queue_items(self):
        """获取队列项"""
        if self._task_system and hasattr(self._task_system, '_queue'):
            try:
                items = self._task_system._queue.get_pending()
                result = []
                for item in items:
                    config = self._task_system.get_task(item.task_id)
                    result.append({
                        "item": item,
                        "config": config,
                    })
                return result
            except Exception:
                return []
        return []
    
    def _update_columns(self):
        """更新各列显示"""
        for col_key, col_widget in self._columns.items():
            items = self._column_data.get(col_key, [])
            
            # 更新计数
            count_label = col_widget._count_label
            count_label.setText(str(len(items)))
            
            # 清空列表
            layout = col_widget._list_layout
            while layout.count() > 0:
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            
            # 添加任务项
            for task_info in items[:10]:  # 最多显示 10 个
                config = task_info["config"]
                task_item = self._create_task_item(config, col_key, task_info)
                layout.insertWidget(layout.count() - 1, task_item)
            
            # 空状态提示
            if not items:
                empty = QLabel("暂无任务", col_widget._list_widget)
                empty.setFont(get_unified_font(9))
                empty.setStyleSheet("color: #555555;")
                empty.setAlignment(Qt.AlignCenter)
                layout.insertWidget(0, empty)
    
    def _create_task_item(self, config: TaskConfig, status: str, task_info: dict) -> QFrame:
        """创建任务项"""
        item = QFrame(self)
        
        # 状态颜色
        colors = {
            "pending": "#6b7280",
            "queued": "#eab308",
            "running": "#22c55e",
            "completed": "#3b82f6",
        }
        border_color = colors.get(status, "#666666")
        
        item.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(50, 50, 55, 150);
                border-left: 2px solid {border_color};
                border-radius: 4px;
                padding: 6px 8px;
            }}
        """)
        
        layout = QVBoxLayout(item)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        
        # 任务名称
        name_label = QLabel(config.name or "未命名", item)
        name_label.setFont(get_unified_font(9, True))
        name_label.setStyleSheet("color: #e0e0e0;")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)
        
        # 任务类型
        type_label = QLabel(f"类型: {config.type.value if hasattr(config.type, 'value') else config.type}", item)
        type_label.setFont(get_unified_font(8))
        type_label.setStyleSheet("color: #888888;")
        layout.addWidget(type_label)
        
        # 操作按钮
        if status == "pending":
            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(4)
            
            trigger_btn = QPushButton("▶ 开始", item)
            trigger_btn.setFont(get_unified_font(8))
            trigger_btn.setFixedHeight(20)
            trigger_btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(34, 197, 94, 150);
                    border: none;
                    border-radius: 4px;
                    color: #22c55e;
                }
                QPushButton:hover {
                    background-color: rgba(34, 197, 94, 200);
                }
            """)
            file_path = task_info.get("file_path")
            if file_path:
                trigger_btn.clicked.connect(lambda _, fp=file_path: self._trigger_file(fp))
            
            btn_layout.addWidget(trigger_btn)
            btn_layout.addStretch()
            layout.addLayout(btn_layout)
        
        elif status == "running":
            # 执行中动画
            status_layout = QHBoxLayout()
            spinner = QLabel("⏳", item)
            spinner.setFont(get_unified_font(10))
            
            status_text = QLabel("执行中...", item)
            status_text.setFont(get_unified_font(8))
            status_text.setStyleSheet("color: #22c55e;")
            
            status_layout.addWidget(spinner)
            status_layout.addWidget(status_text)
            status_layout.addStretch()
            layout.addLayout(status_layout)
        
        elif status == "completed":
            # 完成状态
            completed_status = task_info.get("status", "completed")
            if completed_status == "completed":
                icon = "✅"
                color = "#22c55e"
            else:
                icon = "❌"
                color = "#ef4444"
            
            status_layout = QHBoxLayout()
            status_label = QLabel(f"{icon} {completed_status}", item)
            status_label.setFont(get_unified_font(8))
            status_label.setStyleSheet(f"color: {color};")
            
            status_layout.addWidget(status_label)
            status_layout.addStretch()
            layout.addLayout(status_layout)
        
        return item
    
    def _trigger_file(self, file_path: str):
        """触发文件中的任务"""
        if not self._task_system:
            return
        
        try:
            config = self._get_cached_config(file_path)
            if config:
                self._task_system.enqueue_task(config, trigger_type="manual")
                self.refresh()
        except Exception as e:
            from loguru import logger
            logger.error(f"[TaskQueueCard] 触发任务失败: {e}")