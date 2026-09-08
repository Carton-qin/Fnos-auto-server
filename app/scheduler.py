import random
import logging
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from app.config import BEIJING_TZ, format_now, get_now_beijing
from app.models import Task, TaskLog

logger = logging.getLogger("fnos.scheduler")
# 使用原生 UTC+8 规范化时区对象，杜绝轻量化 Docker 容器中缺少系统 tzdata 导致崩溃的问题
SHANGHAI_TZ = BEIJING_TZ


class TaskScheduler:
    def __init__(self, db_session_factory, executor, notifier):
        self.db_session_factory = db_session_factory
        self.executor = executor
        self.notifier = notifier
        self.scheduler = AsyncIOScheduler(timezone=SHANGHAI_TZ)
        self.task_window_targets = {}  # 记录每日窗口任务的具体触发时间

    async def start(self):
        self.scheduler.start()
        logger.info("[Scheduler] AsyncIOScheduler started with Asia/Shanghai (UTC+8) timezone.")

        # 注册系统日常维护任务：每天 00:01 重新规划当日窗口任务触发时刻
        self.scheduler.add_job(
            self._recalculate_window_targets,
            CronTrigger(hour=0, minute=1, timezone=SHANGHAI_TZ),
            id="sys_window_recalc",
            replace_existing=True
        )

        # 注册每周日早 09:00 自动体检与周报任务
        self.scheduler.add_job(
            self._run_weekly_system_report,
            CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=SHANGHAI_TZ),
            id="sys_weekly_report",
            replace_existing=True
        )

        # 从数据库加载所有启用的自动化任务
        await self.reload_all_tasks()

    async def reload_all_tasks(self):
        async with self.db_session_factory() as session:
            result = await session.execute(select(Task).where(Task.enabled == True))
            tasks = result.scalars().all()
            logger.info(f"[Scheduler] Loading {len(tasks)} enabled tasks from database...")
            for task in tasks:
                await self.schedule_task(task)

    async def schedule_task(self, task: Task):
        job_id = f"task_{task.id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        if not task.enabled:
            return

        stype = task.schedule_type or "cron"

        try:
            if stype == "cron":
                cron_str = (task.cron_expr or "0 8 * * *").strip()
                trigger = CronTrigger.from_crontab(cron_str, timezone=SHANGHAI_TZ)
                self.scheduler.add_job(
                    self._job_wrapper,
                    trigger,
                    args=[task.id, task.jitter_mins],
                    id=job_id,
                    replace_existing=True
                )
                logger.info(f"[Scheduler] Scheduled task {task.name} ({task.id}) with cron: {cron_str}")

            elif stype == "window":
                # 当日执行时刻计算与预定
                await self._schedule_window_job_for_task(task)

        except Exception as e:
            logger.error(f"[Scheduler] Failed to schedule task {task.name} ({task.id}): {e}")

    async def _schedule_window_job_for_task(self, task: Task):
        w_start = task.window_start or "08:00"
        w_end = task.window_end or "09:00"
        try:
            s_h, s_m = map(int, w_start.split(":"))
            e_h, e_m = map(int, w_end.split(":"))
            s_min = s_h * 60 + s_m
            e_min = e_h * 60 + e_m
            if e_min <= s_min:
                e_min = s_min + 60

            target_min = random.randint(s_min, e_min)
            t_h = target_min // 60
            t_m = target_min % 60
            target_time_str = f"{t_h:02d}:{t_m:02d}"
            self.task_window_targets[task.id] = target_time_str

            # 调度今天的运行时间
            now = get_now_beijing()
            run_dt = now.replace(hour=t_h, minute=t_m, second=random.randint(0, 59), microsecond=0)
            if run_dt <= now:
                # 若今天该时刻已过，则预定明天
                run_dt += timedelta(days=1)

            job_id = f"task_{task.id}"
            self.scheduler.add_job(
                self._job_wrapper,
                DateTrigger(run_date=run_dt, timezone=SHANGHAI_TZ),
                args=[task.id, 0],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"[Scheduler] Scheduled window task {task.name} target run at: {run_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            logger.error(f"[Scheduler] Error calculating window target for {task.name}: {e}")

    async def _recalculate_window_targets(self):
        logger.info("[Scheduler] Daily recalc: updating all window task targets for today...")
        async with self.db_session_factory() as session:
            result = await session.execute(select(Task).where(Task.enabled == True, Task.schedule_type == "window"))
            window_tasks = result.scalars().all()
            for task in window_tasks:
                await self._schedule_window_job_for_task(task)

    async def _job_wrapper(self, task_id: str, jitter_mins: int = 0):
        jitter_info = ""
        if jitter_mins and jitter_mins > 0:
            delay_sec = random.randint(0, jitter_mins * 60)
            jitter_info = f"{delay_sec // 60}分{delay_sec % 60}秒"
            logger.info(f"[Scheduler] Applying anti-ban jitter delay: {jitter_info} for task {task_id}")
            import asyncio
            await asyncio.sleep(delay_sec)

        await self.executor.run_task(task_id, jitter_info)

    async def trigger_now(self, task_id: str):
        import asyncio
        asyncio.create_task(self.executor.run_task(task_id, "手动触发"))

    def remove_task(self, task_id: str):
        job_id = f"task_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info(f"[Scheduler] Removed job {job_id}")
        self.task_window_targets.pop(task_id, None)

    async def _run_weekly_system_report(self):
        logger.info("[Scheduler] Generating weekly system health check report...")
        report = await self.generate_weekly_report()
        await self.notifier.send("【飞牛 NAS 系统健康周报】", report, level="urgent")

    async def generate_weekly_report(self) -> str:
        async with self.db_session_factory() as session:
            tasks_res = await session.execute(select(Task))
            all_tasks = tasks_res.scalars().all()

            total_tasks = len(all_tasks)
            enabled_tasks = sum(1 for t in all_tasks if t.enabled)
            tripped_tasks = sum(1 for t in all_tasks if (t.stats or {}).get("circuit_tripped"))

            lines = [
                f"📊 飞牛自动化中枢健康周报 ({format_now()})",
                "----------------------------------------",
                f"📌 任务总数：{total_tasks} 个（运行中：{enabled_tasks}，熔断中：{tripped_tasks}）",
                ""
            ]

            for t in all_tasks:
                stats = t.stats or {}
                streak = stats.get("streak", 0)
                tot_s = stats.get("total_success", 0)
                status_icon = "✅" if t.last_status == "success" else ("🛡️" if stats.get("circuit_tripped") else "❌")
                if t.type == "checkin":
                    lines.append(f"{status_icon} 【{t.name}】: 连续打卡 {streak} 天 | 达标 {tot_s} 次 | 上次执行: {t.last_run or '无'}")
                else:
                    lines.append(f"{status_icon} 【{t.name}】: 状态: {t.last_status or '未执行'} | 上次执行: {t.last_run or '无'}")

            lines.append("----------------------------------------")
            lines.append("💡 运行环境：飞牛私有云 (fnOS) Docker 容器化版本")
            return "\n".join(lines)
