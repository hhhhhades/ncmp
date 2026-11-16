import time
from typing import Dict, Tuple

from ..signer import Signer, beijing_date, ensure_score_date
from .base import BaseTask


class DailyTask(BaseTask):
    def __init__(self, session, logger, config, score_date=None):
        super().__init__(session, logger, config)
        self.score_date = score_date or beijing_date()
        self.result = None
        self.api = {
            "task_data": "https://interface.music.163.com/api/music/partner/daily/task/get"
        }

    def execute(self) -> bool:
        try:
            self.process_and_verify()
            return True
        except Exception as error:
            self.logger.error(f"执行每日任务失败: {error}")
            return False

    def process_and_verify(self) -> Dict:
        """Only declare completion after reading the service's completion state."""
        ensure_score_date(self.score_date)
        complete, initial = self._get_daily_tasks()
        stats = {"submitted": 0, "skipped": 0}
        if not complete:
            stats = self._process_tasks(initial)
            for attempt in range(3):
                complete, final = self._get_daily_tasks()
                if final["id"] != initial["id"]:
                    raise RuntimeError("每日任务 ID 已改变，不能确认同一日任务完成")
                if complete:
                    break
                if attempt < 2:
                    time.sleep(attempt + 1)
            if not complete:
                raise RuntimeError("服务端尚未确认每日评分全部完成，不能记录成功")
        else:
            final = initial
        ensure_score_date(self.score_date)
        self.result = {
            "status": "completed",
            "total": final["count"],
            "completed_before": initial["completedCount"],
            "completed_after": final["completedCount"],
            **stats,
        }
        return {"task_id": initial["id"], **self.result}

    def _get_daily_tasks(self) -> Tuple[bool, Dict]:
        ensure_score_date(self.score_date)
        response = self.session.get(url=self.api["task_data"], timeout=(10, 45)).json()
        if not isinstance(response, dict) or response.get("code") != 200:
            raise RuntimeError("获取每日任务失败：服务端未返回成功状态")
        task_data = response.get("data")
        if not isinstance(task_data, dict) or not task_data.get("id"):
            raise RuntimeError("获取每日任务失败：缺少任务数据或任务 ID")
        count = task_data.get("count")
        completed_count = task_data.get("completedCount")
        works = task_data.get("works", [])
        if (
            type(count) is not int or type(completed_count) is not int
            or not 0 <= completed_count <= count or not isinstance(works, list)
        ):
            raise RuntimeError("获取每日任务失败：任务完成状态无效")
        seen = set()
        for task in works:
            if not isinstance(task, dict) or type(task.get("completed")) is not bool:
                raise RuntimeError("获取每日任务失败：作品完成状态无效")
            work = task.get("work")
            if not isinstance(work, dict) or "id" not in work:
                raise RuntimeError("获取每日任务失败：缺少作品 ID")
            work_id = str(work["id"])
            if work_id in seen:
                raise RuntimeError("获取每日任务失败：作品 ID 重复")
            seen.add(work_id)
        task_data["works"] = works
        complete = count == completed_count
        self.logger.info(f'今日任务：{"已完成" if complete else "未完成"}[{completed_count}/{count}]')
        return complete, task_data

    def _process_tasks(self, task_data: Dict) -> Dict:
        self.logger.info("开始评分...")
        signer = Signer(self.session, task_data["id"], self.logger, self.config, self.score_date)
        stats = {"submitted": 0, "skipped": 0}
        for task in task_data["works"]:
            if task["completed"]:
                continue
            ensure_score_date(self.score_date)
            if signer.sign(task["work"]):
                stats["submitted"] += 1
            else:
                stats["skipped"] += 1
        return stats
