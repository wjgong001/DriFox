# -*- coding: utf-8 -*-
"""
任务队列管理
支持优先级排序、状态管理、重试机制
"""

from datetime import datetime
from typing import List, Optional, Callable
from loguru import logger

from .database import Database
from .models import QueueItem, QueueStatus, TaskConfig


class TaskQueue:
    """任务队列管理
    
    支持优先级排序、状态管理、重试机制
    """

    def __init__(self, db: Optional[Database] = None):
        """初始化任务队列
        
        Args:
            db: 数据库实例，默认使用单例
        """
        self._db = db or Database.get_instance()

    def enqueue(self, config: TaskConfig, trigger_type: str = "manual") -> int:
        """将任务加入队列
        
        Args:
            config: 任务配置
            trigger_type: 触发类型 (scheduled/manual/file_change)
            
        Returns:
            队列项 ID
        """
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 计算优先级
            priority = self._calc_priority(config)
            
            # 执行时间
            execute_at = None
            if config.trigger.execute_at and config.trigger.execute_at != "now":
                execute_at = config.trigger.execute_at
            
            # 获取最大优先级和最小创建时间的ID，用于插入顺序
            self._db.execute(
                """
                INSERT INTO task_queue 
                (task_id, trigger_type, priority, status, execute_at, created_at, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.id,
                    trigger_type,
                    priority,
                    QueueStatus.PENDING.value,
                    execute_at,
                    now,
                    0
                )
            )
            self._db.commit()
            
            # 获取插入的 ID
            cursor = self._db.execute("SELECT last_insert_rowid()")
            queue_id = cursor.fetchone()[0]
            
            logger.debug(f"[TaskQueue] 入队: task_id={config.id}, queue_id={queue_id}, priority={priority}")
            return queue_id
        except Exception as e:
            logger.error(f"[TaskQueue] 入队失败: {e}")
            self._db.rollback()
            return -1

    def _calc_priority(self, config: TaskConfig) -> int:
        """计算任务优先级
        
        Args:
            config: 任务配置
            
        Returns:
            优先级值（越大优先级越高）
        """
        base = 0
        
        # 基于 priority 字段
        priority_map = {"high": 100, "normal": 50, "low": 0}
        base += priority_map.get(config.priority, 50)
        
        # 基于触发类型
        trigger_priority = {
            "file_change": 80,  # 文件变化立即处理
            "manual": 60,       # 手动触发次之
            "scheduled": 40     # 定时任务最后
        }
        base += trigger_priority.get(config.trigger.mode.value, 50)
        
        return base

    def dequeue(self) -> Optional[QueueItem]:
        """取出最高优先级的待处理任务
        
        Returns:
            队列项或 None
        """
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 查询条件：pending 状态，且执行时间 <= 当前时间
            row = self._db.fetch_one(
                """
                SELECT * FROM task_queue 
                WHERE status = ? AND (execute_at IS NULL OR execute_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (QueueStatus.PENDING.value, now)
            )
            
            if not row:
                return None
            
            item = QueueItem.from_dict(dict(row))
            return item
        except Exception as e:
            logger.error(f"[TaskQueue] 出队失败: {e}")
            return None

    def update_status(
        self,
        queue_id: int,
        status: QueueStatus,
        error: Optional[str] = None
    ) -> bool:
        """更新队列项状态
        
        Args:
            queue_id: 队列项 ID
            status: 新状态
            error: 错误信息（可选）
            
        Returns:
            是否成功
        """
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if status == QueueStatus.RUNNING:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, started_at = ? WHERE id = ?",
                    (status.value, now, queue_id)
                )
            elif status in (QueueStatus.COMPLETED, QueueStatus.FAILED):
                self._db.execute(
                    "UPDATE task_queue SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                    (status.value, now, error, queue_id)
                )
            else:
                self._db.execute(
                    "UPDATE task_queue SET status = ? WHERE id = ?",
                    (status.value, queue_id)
                )
            
            self._db.commit()
            logger.debug(f"[TaskQueue] 更新状态: id={queue_id}, status={status.value}")
            return True
        except Exception as e:
            logger.error(f"[TaskQueue] 更新状态失败: {e}")
            self._db.rollback()
            return False

    def increment_retry(self, queue_id: int) -> int:
        """增加重试计数
        
        Args:
            queue_id: 队列项 ID
            
        Returns:
            新的重试计数
        """
        try:
            cursor = self._db.execute(
                "UPDATE task_queue SET retry_count = retry_count + 1 WHERE id = ?",
                (queue_id,)
            )
            self._db.commit()
            
            # 获取新的重试计数
            row = self._db.fetch_one(
                "SELECT retry_count FROM task_queue WHERE id = ?",
                (queue_id,)
            )
            return row["retry_count"] if row else 0
        except Exception as e:
            logger.error(f"[TaskQueue] 增加重试计数失败: {e}")
            return 0

    def get_pending_count(self) -> int:
        """获取待处理任务数量
        
        Returns:
            待处理任务数量
        """
        try:
            row = self._db.fetch_one(
                "SELECT COUNT(*) as cnt FROM task_queue WHERE status = ?",
                (QueueStatus.PENDING.value,)
            )
            return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"[TaskQueue] 获取待处理数量失败: {e}")
            return 0

    def get_running_count(self) -> int:
        """获取运行中任务数量
        
        Returns:
            运行中任务数量
        """
        try:
            row = self._db.fetch_one(
                "SELECT COUNT(*) as cnt FROM task_queue WHERE status = ?",
                (QueueStatus.RUNNING.value,)
            )
            return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"[TaskQueue] 获取运行中数量失败: {e}")
            return 0

    def get_all_pending(self) -> List[QueueItem]:
        """获取所有待处理任务
        
        Returns:
            队列项列表
        """
        try:
            rows = self._db.fetch_all(
                """
                SELECT * FROM task_queue 
                WHERE status = ?
                ORDER BY priority DESC, created_at ASC
                """,
                (QueueStatus.PENDING.value,)
            )
            return [QueueItem.from_dict(dict(row)) for row in rows]
        except Exception as e:
            logger.error(f"[TaskQueue] 获取待处理任务失败: {e}")
            return []

    def get_by_task_id(self, task_id: str) -> List[QueueItem]:
        """获取指定任务的所有队列项
        
        Args:
            task_id: 任务 ID
            
        Returns:
            队列项列表
        """
        try:
            rows = self._db.fetch_all(
                "SELECT * FROM task_queue WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,)
            )
            return [QueueItem.from_dict(dict(row)) for row in rows]
        except Exception as e:
            logger.error(f"[TaskQueue] 获取任务队列项失败: {e}")
            return []

    def clear_completed(self, older_than_hours: int = 24) -> int:
        """清理已完成的旧队列项
        
        Args:
            older_than_hours: 保留最近多少小时内完成的任务
            
        Returns:
            清理数量
        """
        try:
            from datetime import timedelta
            
            cutoff_time = datetime.now() - timedelta(hours=older_than_hours)
            cursor = self._db.execute(
                """
                DELETE FROM task_queue 
                WHERE status IN (?, ?) AND completed_at < ?
                """,
                (QueueStatus.COMPLETED.value, QueueStatus.FAILED.value, cutoff_time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self._db.commit()
            count = cursor.rowcount
            logger.debug(f"[TaskQueue] 清理完成: {count} 条")
            return count
        except Exception as e:
            logger.error(f"[TaskQueue] 清理队列失败: {e}")
            return 0

    def cancel(self, queue_id: int) -> bool:
        """取消队列项
        
        Args:
            queue_id: 队列项 ID
            
        Returns:
            是否成功
        """
        try:
            self._db.execute(
                "DELETE FROM task_queue WHERE id = ? AND status = ?",
                (queue_id, QueueStatus.PENDING.value)
            )
            self._db.commit()
            logger.debug(f"[TaskQueue] 取消队列项: {queue_id}")
            return True
        except Exception as e:
            logger.error(f"[TaskQueue] 取消队列项失败: {e}")
            self._db.rollback()
            return False

    def requeue_failed(self, max_retries: int = 3) -> int:
        """重新入队失败任务（未超过最大重试次数的）
        
        Args:
            max_retries: 最大重试次数
            
        Returns:
            重新入队的数量
        """
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 找出失败且未超过重试次数的任务
            rows = self._db.fetch_all(
                """
                SELECT * FROM task_queue 
                WHERE status = ? AND retry_count < ?
                """,
                (QueueStatus.FAILED.value, max_retries)
            )
            
            count = 0
            for row in rows:
                self._db.execute(
                    """
                    UPDATE task_queue 
                    SET status = ?, execute_at = NULL, error = NULL
                    WHERE id = ?
                    """,
                    (QueueStatus.PENDING.value, row["id"])
                )
                count += 1
            
            self._db.commit()
            logger.info(f"[TaskQueue] 重新入队: {count} 个任务")
            return count
        except Exception as e:
            logger.error(f"[TaskQueue] 重新入队失败: {e}")
            self._db.rollback()
            return 0