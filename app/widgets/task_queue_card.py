# -*- coding: utf-8 -*-
"""
任务队列卡片
显示当前任务队列状态，支持查看和管理任务
"""

import os
import time
from datetime import datetime
from typing import Dict, Any
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QScrollArea, QFrame, QPushButton, QSizePolicy
)
from PyQt5.QtGui import QFont

from app.utils.utils import get_unified_font, get_icon
from app.core.task_watcher import (
    TaskWatcherSystem,
    TaskConfig,
    TaskResult,
    QueueStatus,
)


class TaskQueueCard(QFrame):
    """任务队列卡片
    
    显示待处理、执行中、已完成的任务列表
    """
    
    # 信号
    task_triggered = pyqtSignal(str)  # 任务被触发
    refresh_requested = pyqtSignal()  # 刷新请求
    
    def __init__(self, task_system: TaskWatcherSystem = None, parent=None):
        super().__init__(parent)
        
        self._task_system = task_system
        self._cached_tasks: Dict[str, Any] = {}  # 文件路径 -> {config, mtime}
        self._setup_ui()
    
    def set_task_system(self, system):
        """设置任务系统"""
        # 避免错误设置
        if system is None or hasattr(system, 'enqueue_task'):
            self._task_system = system
        else:
            self._task_system = None
    
    def refresh(self):
        """刷新显示"""
        self._refresh_display()
        self._refresh_display()
    
    def _setup_ui(self):
        """设置 UI"""
        self.setFixedHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("""
            TaskQueueCard {
                background-color: rgba(33, 33, 38, 250);
                border: 1px solid #3d3d3d;
                border-radius: 8px;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        
        # 标题栏
        header = QHBoxLayout()
        header.setSpacing(8)
        
        title_label = QLabel("📋 任务队列", self)
        title_label.setFont(get_unified_font(11, True))
        title_label.setStyleSheet("color: #f59e0b;")
        header.addWidget(title_label)
        
        header.addStretch()
        
        # 刷新按钮
        refresh_btn = QPushButton("🔄", self)
        refresh_btn.setFont(get_unified_font(10))
        refresh_btn.setFixedSize(28, 24)
        refresh_btn.setToolTip("刷新")
        refresh_btn.clicked.connect(self._refresh_display)
        header.addWidget(refresh_btn)
        
        # 关闭按钮
        close_btn = QPushButton("✕", self)
        close_btn.setFont(get_unified_font(10))
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("color: #888888; background: transparent; border: none;")
        close_btn.clicked.connect(lambda: self.hide())
        header.addWidget(close_btn)
        
        layout.addLayout(header)
        
        # 状态摘要
        status_layout = QHBoxLayout()
        status_layout.setSpacing(12)
        
        # 待处理数量
        self.pending_label = self._create_status_chip("待处理", "0", "#eab308")
        status_layout.addWidget(self.pending_label)
        
        # 执行中数量
        self.running_label = self._create_status_chip("执行中", "0", "#22c55e")
        status_layout.addWidget(self.running_label)
        
        # 调度任务数量
        self.scheduled_label = self._create_status_chip("已调度", "0", "#3b82f6")
        status_layout.addWidget(self.scheduled_label)
        
        layout.addLayout(status_layout)
        
        # 任务列表区域
        list_label = QLabel("任务列表", self)
        list_label.setFont(get_unified_font(10, True))
        list_label.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(list_label)
        
        self.task_list_widget = QWidget(self)
        self.task_list_layout = QVBoxLayout(self.task_list_widget)
        self.task_list_layout.setContentsMargins(0, 4, 0, 4)
        self.task_list_layout.setSpacing(4)
        self.task_list_layout.addStretch()
        
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("""
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
        """)
        scroll_area.setWidget(self.task_list_widget)
        layout.addWidget(scroll_area, 1)
        
        # 初始化显示
        self._refresh_display()
    
    def _create_status_chip(self, name: str, value: str, color: str) -> QWidget:
        """创建状态芯片"""
        chip = QFrame(self)
        chip.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(50, 50, 50, 200);
                border: 1px solid {color}40;
                border-radius: 12px;
                padding: 4px 12px;
            }}
        """)
        
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        
        name_label = QLabel(name, chip)
        name_label.setFont(get_unified_font(9))
        name_label.setStyleSheet("color: #888888;")
        
        value_label = QLabel(value, chip)
        value_label.setFont(get_unified_font(10, True))
        value_label.setStyleSheet(f"color: {color};")
        value_label.setObjectName("value_label")
        
        layout.addWidget(name_label)
        layout.addWidget(value_label)
        
        return chip
    
    def _refresh_display(self):
        """刷新显示"""
        print("[TaskQueueCard] _refresh_display 被调用")
        # 总是先尝试显示文件系统中的任务
        self._show_filesystem_tasks()
    
    def _show_filesystem_tasks(self):
        """显示文件系统中的任务文件"""
        from app.core.task_watcher import TaskParser
        from app.core.task_watcher.database import TaskWatcherDB
        
        # 清除现有任务项
        while self.task_list_layout.count() > 1:
            item = self.task_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # 更新状态
        self._update_chip_value(self.pending_label, "-")
        self._update_chip_value(self.running_label, "-")
        self._update_chip_value(self.scheduled_label, "-")
        
        # 获取任务目录
        tasks_dir = os.path.abspath(os.path.join(".drifox", "tasks"))
        
        # 调试信息
        print(f"[TaskQueueCard] 扫描目录: {tasks_dir}")
        print(f"[TaskQueueCard] 目录存在: {os.path.exists(tasks_dir)}")
        
        if not os.path.exists(tasks_dir):
            empty_label = QLabel("任务目录不存在", self.task_list_widget)
            empty_label.setFont(get_unified_font(10))
            empty_label.setStyleSheet("color: #666666;")
            empty_label.setAlignment(Qt.AlignCenter)
            self.task_list_layout.insertWidget(0, empty_label)
            return
        
        # 确保数据库初始化
        try:
            db = TaskWatcherDB.get_instance()
            db.ensure_initialized()
        except Exception:
            pass
        
        # 扫描任务文件
        parser = TaskParser()
        task_count = 0
        current_files = set()
        
        try:
            files = os.listdir(tasks_dir)
            print(f"[TaskQueueCard] 文件列表: {len(files)} 个")
        except Exception as e:
            print(f"[TaskQueueCard] 列出文件失败: {e}")
            files = []
        
        for file in files:
            if not file.endswith(".task.md"):
                continue
            
            file_path = os.path.join(tasks_dir, file)
            current_files.add(file_path)
            
            try:
                config = parser.parse_file(file_path)
                if config:
                    print(f"[TaskQueueCard] 解析成功: {file} -> {config.name}")
                    task_item = self._create_task_item(config, "file")
                    self.task_list_layout.insertWidget(
                        self.task_list_layout.count() - 1,
                        task_item
                    )
                    task_count += 1
                    if task_count >= 20:
                        break
                else:
                    print(f"[TaskQueueCard] 解析失败: {file}")
            except Exception as e:
                print(f"[TaskQueueCard] 处理文件出错 {file}: {e}")
                continue
        
        # 清理已删除文件的缓存
        cached_paths = set(self._cached_tasks.keys())
        for deleted_path in cached_paths - current_files:
            if deleted_path in self._cached_tasks:
                del self._cached_tasks[deleted_path]
        
        print(f"[TaskQueueCard] 共显示 {task_count} 个任务")
        if task_count == 0:
            empty_label = QLabel("暂无任务文件", self.task_list_widget)
            empty_label.setFont(get_unified_font(10))
            empty_label.setStyleSheet("color: #666666;")
            empty_label.setAlignment(Qt.AlignCenter)
            self.task_list_layout.insertWidget(0, empty_label)
    
    def trigger_file_task(self, config: TaskConfig):
        """触发文件中的任务（需要 TaskWatcher 初始化）"""
        if not self._task_system:
            from loguru import logger
            logger.error("[TaskQueueCard] TaskWatcher 未初始化，无法触发任务")
            return False
        
        try:
            self._task_system.enqueue_task(config, trigger_type="manual")
            from loguru import logger
            logger.info(f"[TaskQueueCard] 触发任务: {config.name}")
            self._refresh_display()
            return True
        except Exception as e:
            from loguru import logger
            logger.error(f"[TaskQueueCard] 触发任务失败: {e}")
            return False
    
    def _update_chip_value(self, chip: QFrame, value: str):
        """更新芯片数值"""
        value_label = chip.findChild(QLabel, "value_label")
        if value_label:
            value_label.setText(value)
    
    def _update_task_list(self):
        """更新任务列表"""
        # 清除现有任务项
        while self.task_list_layout.count() > 1:
            item = self.task_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # 获取待处理和运行中的任务
        queue = self._task_system._queue
        
        # 获取队列中的任务
        pending_items = queue.get_all_pending()
        running_count = self._task_system.running_count
        
        # 添加待处理任务
        if pending_items:
            for item in pending_items[:10]:  # 最多显示 10 个
                config = self._task_system.get_task(item.task_id)
                if config:
                    task_item = self._create_task_item(config, item.status.value)
                    self.task_list_layout.insertWidget(
                        self.task_list_layout.count() - 1,
                        task_item
                    )
        
        # 添加运行中的任务
        if running_count > 0:
            running_item = self._create_running_item()
            self.task_list_layout.insertWidget(
                self.task_list_layout.count() - 1,
                running_item
            )
        
        # 空状态提示
        if pending_items == [] and running_count == 0:
            empty_label = QLabel("暂无任务", self.task_list_widget)
            empty_label.setFont(get_unified_font(10))
            empty_label.setStyleSheet("color: #666666;")
            empty_label.setAlignment(Qt.AlignCenter)
            self.task_list_layout.insertWidget(0, empty_label)
    
    def _get_type_value(self, config):
        """安全获取类型值"""
        if hasattr(config.type, 'value'):
            return config.type.value
        return str(config.type)
    
    def _get_trigger_mode(self, config):
        """安全获取触发模式"""
        if hasattr(config.trigger, 'mode'):
            if hasattr(config.trigger.mode, 'value'):
                return config.trigger.mode.value
            return str(config.trigger.mode)
        return "unknown"
    
    def _create_task_item(self, config: TaskConfig, status: str) -> QWidget:
        """创建任务项"""
        item = QFrame(self.task_list_widget)
        
        # 状态颜色
        status_colors = {
            "pending": "#eab308",
            "running": "#22c55e",
            "completed": "#888888",
            "failed": "#ef4444",
            "file": "#3b82f6",
        }
        status_color = status_colors.get(status, "#888888")
        
        # 状态标签
        status_labels = {
            "pending": "● 待处理",
            "running": "⏳ 执行中",
            "completed": "✓ 已完成",
            "failed": "✗ 失败",
            "file": "📄 文件",
        }
        status_text = status_labels.get(status, status)
        
        item.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(40, 40, 45, 180);
                border: 1px solid rgba(60, 60, 65, 150);
                border-radius: 6px;
                padding: 8px;
            }}
            QFrame:hover {{
                background-color: rgba(50, 50, 55, 200);
            }}
        """)
        
        layout = QVBoxLayout(item)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        
        # 任务名称和状态
        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)
        
        name_label = QLabel(config.name or config.id[:8], item)
        name_label.setFont(get_unified_font(10, True))
        name_label.setStyleSheet("color: #ffffff;")
        
        status_label = QLabel(status_text, item)
        status_label.setFont(get_unified_font(9))
        status_label.setStyleSheet(f"color: {status_color};")
        
        top_layout.addWidget(name_label)
        top_layout.addStretch()
        top_layout.addWidget(status_label)
        
        layout.addLayout(top_layout)
        
        # 任务类型和触发方式
        type_value = self._get_type_value(config)
        trigger_mode = self._get_trigger_mode(config)
        
        type_label = QLabel(f"类型: {type_value}", item)
        type_label.setFont(get_unified_font(9))
        type_label.setStyleSheet("color: #888888;")
        
        trigger_label = QLabel(f"触发: {trigger_mode}", item)
        trigger_label.setFont(get_unified_font(9))
        trigger_label.setStyleSheet("color: #666666;")
        
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(8)
        bottom_layout.addWidget(type_label)
        bottom_layout.addWidget(trigger_label)
        
        # 对于文件中的任务，显示执行按钮
        if status == "file" and self._task_system:
            trigger_btn = QPushButton("▶ 执行", item)
            trigger_btn.setFont(get_unified_font(9))
            trigger_btn.setFixedSize(50, 20)
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
            trigger_btn.clicked.connect(lambda _, c=config: self.trigger_file_task(c))
            bottom_layout.addWidget(trigger_btn)
        elif status == "file":
            # TaskWatcher 未初始化，显示提示
            hint_label = QLabel("需先初始化", item)
            hint_label.setFont(get_unified_font(9))
            hint_label.setStyleSheet("color: #888888;")
            bottom_layout.addWidget(hint_label)
        
        bottom_layout.addStretch()
        
        layout.addLayout(bottom_layout)
        
        return item
    
    def _create_running_item(self) -> QWidget:
        """创建运行中状态项"""
        item = QFrame(self.task_list_widget)
        item.setStyleSheet("""
            QFrame {
                background-color: rgba(34, 197, 94, 50);
                border: 1px solid rgba(34, 197, 94, 100);
                border-radius: 6px;
                padding: 8px;
            }
        """)
        
        layout = QHBoxLayout(item)
        layout.setContentsMargins(8, 6, 8, 6)
        
        spinner_label = QLabel("⏳", item)
        spinner_label.setFont(get_unified_font(12))
        
        text_label = QLabel("任务执行中...", item)
        text_label.setFont(get_unified_font(10))
        text_label.setStyleSheet("color: #22c55e;")
        
        layout.addWidget(spinner_label)
        layout.addWidget(text_label)
        layout.addStretch()
        
        return item
    
    def _clear_completed(self):
        """清理已完成任务"""
        if self._task_system:
            count = self._task_system.clear_completed_tasks(older_than_hours=0)
            from loguru import logger
            logger.info(f"[TaskQueueCard] 清理了 {count} 个已完成任务")
            self._refresh_display()


class TaskQueueCardSimple(QWidget):
    """简化版任务队列显示（用于嵌入其他卡片）"""
    
    def __init__(self, task_system: TaskWatcherSystem = None, parent=None):
        super().__init__(parent)
        
        self._task_system = task_system
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(3000)
        
        self._setup_ui()
    
    def set_task_system(self, system: TaskWatcherSystem):
        self._task_system = system
        self._refresh()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)
        
        # 标题
        title_layout = QHBoxLayout()
        title_label = QLabel("📋 任务队列", self)
        title_label.setFont(get_unified_font(11, True))
        title_label.setStyleSheet("color: #f59e0b;")
        
        self.count_label = QLabel("0 个任务", self)
        self.count_label.setFont(get_unified_font(9))
        self.count_label.setStyleSheet("color: #888888;")
        
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(self.count_label)
        
        layout.addLayout(title_layout)
        
        # 任务列表容器
        self.task_container = QVBoxLayout()
        self.task_container.setSpacing(4)
        layout.addLayout(self.task_container)
        
        layout.addStretch()
        
        self._refresh()
    
    def _refresh(self):
        """刷新"""
        if not self._task_system:
            return
        
        try:
            stats = self._task_system.get_queue_stats()
            total = stats.get("pending", 0) + stats.get("running", 0)
            self.count_label.setText(f"{total} 个任务")
            
            # 清空并重新添加
            while self.task_container.count():
                item = self.task_container.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            
            # 添加任务项
            if stats.get("running", 0) > 0:
                running = QLabel("⏳ 执行中", self)
                running.setFont(get_unified_font(9))
                running.setStyleSheet("color: #22c55e;")
                self.task_container.addWidget(running)
            
            pending = stats.get("pending", 0)
            if pending > 0:
                pending_label = QLabel(f"📥 {pending} 个待处理", self)
                pending_label.setFont(get_unified_font(9))
                pending_label.setStyleSheet("color: #eab308;")
                self.task_container.addWidget(pending_label)
            
        except Exception as e:
            from loguru import logger
            logger.error(f"[TaskQueueCardSimple] 刷新失败: {e}")