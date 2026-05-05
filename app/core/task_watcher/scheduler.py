# -*- coding: utf-8 -*-
"""
任务调度器
使用 APScheduler 管理定时任务，按 cron 表达式触发
"""

import time
from typing import Optional, Callable, Dict
from loguru import logger

# 尝试导入 APScheduler
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    logger.warning("[TaskScheduler] APScheduler 未安装，无法使用定时任务功能")


from .models import TaskConfig, TriggerMode
from .config_store import TaskConfigStore


class TaskScheduler:
    """任务调度器
    
    使用 APScheduler 管理定时任务
    """

    def __init__(self, config_store: Optional[TaskConfigStore] = None):
        """初始化任务调度器
        
        Args:
            config_store: TaskConfigStore 实例
        """
        self._config_store = config_store
        self._scheduler: Optional[BackgroundScheduler] = None
        self._callback: Optional[Callable[[TaskConfig], None]] = None
        self._running = False
        
        # 已调度的任务映射：task_id -> job_id
        self._scheduled_jobs: Dict[str, str] = {}
        
        if not APSCHEDULER_AVAILABLE:
            logger.warning("[TaskScheduler] APScheduler 不可用，定时任务功能将无法使用")

    def set_callback(self, callback: Callable[[TaskConfig], None]) -> None:
        """设置任务触发回调
        
        Args:
            callback: 回调函数，参数为 TaskConfig
        """
        self._callback = callback

    def _on_trigger(self, task_id: str) -> None:
        """定时触发回调
        
        Args:
            task_id: 任务 ID
        """
        if not self._callback:
            return
        
        # 获取任务配置
        config = None
        if self._config_store:
            config = self._config_store.get(task_id)
        
        if not config:
            logger.warning(f"[TaskScheduler] 触发任务但找不到配置: {task_id}")
            return
        
        try:
            self._callback(config)
        except Exception as e:
            logger.error(f"[TaskScheduler] 执行回调失败: {e}")

    def schedule_task(self, config: TaskConfig) -> bool:
        """注册定时任务
        
        Args:
            config: 任务配置
            
        Returns:
            是否成功
        """
        if not APSCHEDULER_AVAILABLE:
            logger.error("[TaskScheduler] APScheduler 不可用，无法调度任务")
            return False
        
        if config.trigger.mode != TriggerMode.SCHEDULED:
            logger.debug(f"[TaskScheduler] 非定时任务，跳过调度: {config.id}")
            return False
        
        try:
            # 解析 cron 表达式
            cron_expr = config.trigger.cron
            if not cron_expr:
                logger.error(f"[TaskScheduler] 定时任务缺少 cron 表达式: {config.id}")
                return False
            
            # 确保调度器已启动
            if not self._scheduler:
                self.start()
            
            # 构建 cron 触发器
            # cron 表达式格式：秒 分 时 日 月 周
            cron_parts = cron_expr.strip().split()
            
            trigger_args = {}
            if len(cron_parts) >= 5:
                trigger_args = {
                    "minute": cron_parts[0],
                    "hour": cron_parts[1],
                    "day": cron_parts[2],
                    "month": cron_parts[3],
                    "day_of_week": cron_parts[4],
                }
                if len(cron_parts) >= 6:
                    trigger_args["second"] = cron_parts[0]
                    trigger_args["minute"] = cron_parts[1]
                    trigger_args["hour"] = cron_parts[2]
                    trigger_args["day"] = cron_parts[3]
                    trigger_args["month"] = cron_parts[4]
                    trigger_args["day_of_week"] = cron_parts[5]
            
            trigger = CronTrigger(**trigger_args)
            
            # 生成 job_id
            job_id = f"task_{config.id}"
            
            # 如果已存在，移除旧任务
            if job_id in self._scheduled_jobs:
                self.unschedule_task(config.id)
            
            # 添加任务
            self._scheduler.add_job(
                func=lambda: self._on_trigger(config.id),
                trigger=trigger,
                id=job_id,
                name=config.name or config.id,
                replace_existing=True,
                misfire_grace_time=60  # 允许60秒的误差
            )
            
            self._scheduled_jobs[config.id] = job_id
            logger.info(f"[TaskScheduler] 调度任务: {config.id}, cron={cron_expr}")
            return True
            
        except Exception as e:
            logger.error(f"[TaskScheduler] 调度任务失败: {config.id}, error={e}")
            return False

    def unschedule_task(self, task_id: str) -> bool:
        """取消定时任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        if not self._scheduler:
            return True
        
        try:
            job_id = self._scheduled_jobs.get(task_id)
            if job_id:
                self._scheduler.remove_job(job_id)
                del self._scheduled_jobs[task_id]
                logger.debug(f"[TaskScheduler] 取消任务调度: {task_id}")
            
            return True
        except Exception as e:
            logger.error(f"[TaskScheduler] 取消任务调度失败: {e}")
            return False

    def trigger_now(self, task_id: str) -> bool:
        """立即触发任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否成功
        """
        self._on_trigger(task_id)
        return True

    def load_scheduled_tasks(self, config_store: TaskConfigStore) -> int:
        """从配置加载所有定时任务
        
        Args:
            config_store: TaskConfigStore 实例
            
        Returns:
            加载的任务数量
        """
        count = 0
        
        # 确保调度器已启动
        if not self._scheduler:
            self.start()
        
        # 获取所有 scheduled 模式的任务
        configs = config_store.get_by_trigger_mode(TriggerMode.SCHEDULED)
        
        for config in configs:
            if config.enabled and config.trigger.mode == TriggerMode.SCHEDULED:
                if self.schedule_task(config):
                    count += 1
        
        logger.info(f"[TaskScheduler] 加载了 {count} 个定时任务")
        return count

    def start(self) -> bool:
        """启动调度器
        
        Returns:
            是否成功
        """
        if self._running:
            logger.debug("[TaskScheduler] 已经在运行中")
            return True
        
        if not APSCHEDULER_AVAILABLE:
            logger.error("[TaskScheduler] 无法启动，APScheduler 不可用")
            return False
        
        try:
            self._scheduler = BackgroundScheduler(
                timezone="Asia/Shanghai",
                job_defaults={
                    "coalesce": True,  # 合并错过的任务
                    "max_instances": 1,  # 同一任务最多一个实例
                }
            )
            self._scheduler.start()
            self._running = True
            logger.info("[TaskScheduler] 调度器启动完成")
            return True
        except Exception as e:
            logger.error(f"[TaskScheduler] 启动失败: {e}")
            return False

    def stop(self) -> bool:
        """停止调度器
        
        Returns:
            是否成功
        """
        if not self._running:
            return True
        
        try:
            if self._scheduler:
                self._scheduler.shutdown(wait=True)
                self._scheduler = None
            
            self._running = False
            self._scheduled_jobs.clear()
            logger.info("[TaskScheduler] 调度器停止完成")
            return True
        except Exception as e:
            logger.error(f"[TaskScheduler] 停止失败: {e}")
            return False

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    @property
    def scheduled_count(self) -> int:
        """已调度的任务数量"""
        return len(self._scheduled_jobs)

    def get_next_run_time(self, task_id: str) -> Optional[str]:
        """获取任务下次执行时间
        
        Args:
            task_id: 任务 ID
            
        Returns:
            下次执行时间的 ISO 字符串，或 None
        """
        if not self._scheduler:
            return None
        
        job_id = self._scheduled_jobs.get(task_id)
        if not job_id:
            return None
        
        try:
            job = self._scheduler.get_job(job_id)
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
        except Exception as e:
            logger.error(f"[TaskScheduler] 获取下次执行时间失败: {e}")
        
        return None

    def get_all_scheduled(self) -> list:
        """获取所有已调度的任务信息
        
        Returns:
            任务信息列表
        """
        result = []
        
        if not self._scheduler:
            return result
        
        for task_id, job_id in self._scheduled_jobs.items():
            try:
                job = self._scheduler.get_job(job_id)
                if job:
                    result.append({
                        "task_id": task_id,
                        "job_id": job_id,
                        "name": job.name,
                        "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                        "pending": job.pending,
                    })
            except Exception as e:
                logger.error(f"[TaskScheduler] 获取任务信息失败: {e}")
        
        return result