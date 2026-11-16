import time
from typing import Dict, List, Tuple

from src.core.signer import Signer, beijing_date, ensure_score_date


class ExtraTask:
    def __init__(self, session, logger, config, score_date=None):
        self.session = session
        self.logger = logger
        self.config = config
        self.score_date = score_date or beijing_date()
        self.api = {
            "extra_list": "https://interface.music.163.com/api/music/partner/extra/wait/evaluate/work/list",
            "report_listen": "https://interface.music.163.com/weapi/partner/resource/interact/report",
        }
        self.signer = Signer(session, "", logger, config, self.score_date)

    def process_extra_tasks(self, task_id: str) -> Dict:
        """Preserve the configured task limit and verify it against server state."""
        ensure_score_date(self.score_date)
        extra_tasks, completed_before = self._get_extra_tasks()
        full = bool(self.config.get("full_extra_tasks", False))
        available = len(extra_tasks) + completed_before
        target = available if full else min(7, available)
        submitted = skipped = failed = 0
        remaining = max(0, target - completed_before)
        seen = set()
        for task in extra_tasks:
            if submitted >= remaining:
                break
            work_id = str(task["work"]["id"])
            if work_id in seen:
                continue
            seen.add(work_id)
            ensure_score_date(self.score_date)
            try:
                if self._process_single_task(task, task_id):
                    submitted += 1
                    self.logger.info(f"成功完成第 {submitted}/{remaining} 个额外评分任务")
                    if submitted < remaining:
                        delay = self.config.get_wait_time()
                        self.logger.info(f"等待 {delay:.1f} 秒后继续...")
                        time.sleep(delay)
                else:
                    skipped += 1
            except Exception as error:
                # A date change aborts immediately; no more tasks may be submitted.
                ensure_score_date(self.score_date)
                failed += 1
                self.logger.warning(f"额外评分未获确认，继续检查其余任务: {error}")

        if submitted or skipped or failed:
            for attempt in range(3):
                pending_after, completed_after = self._get_extra_tasks()
                if full:
                    target = max(target, len(pending_after) + completed_after)
                if completed_after >= target:
                    break
                if attempt < 2:
                    time.sleep(attempt + 1)
        else:
            completed_after = completed_before
        ensure_score_date(self.score_date)
        if completed_after < target:
            raise RuntimeError(
                f"服务端尚未确认额外评分全部完成：{completed_after}/{target}"
                f"；本次确认提交 {submitted}，资源跳过 {skipped}，失败 {failed}"
            )
        self.logger.info(f"额外评分经服务端确认完成：{completed_after}/{target}，本次提交 {submitted} 首")
        return {
            "status": "completed",
            "full_extra_tasks": full,
            "target": target,
            "completed_before": completed_before,
            "completed_after": completed_after,
            "submitted": submitted,
            "skipped": skipped,
            "failed_attempts": failed,
        }

    def _get_extra_tasks(self) -> Tuple[List[Dict], int]:
        ensure_score_date(self.score_date)
        response = self.session.get(
            url=self.api["extra_list"],
            headers={"Referer": "https://mp.music.163.com/"},
            timeout=(10, 45),
        ).json()
        if not isinstance(response, dict) or response.get("code") != 200:
            raise RuntimeError("获取额外任务失败：服务端未返回成功状态")
        tasks = response.get("data")
        if not isinstance(tasks, list):
            raise RuntimeError("获取额外任务失败：任务列表无效")
        seen = set()
        for task in tasks:
            if (
                not isinstance(task, dict) or type(task.get("completed")) is not bool
                or not isinstance(task.get("work"), dict) or "id" not in task["work"]
            ):
                raise RuntimeError("获取额外任务失败：作品或完成状态无效")
            work_id = str(task["work"]["id"])
            if work_id in seen:
                raise RuntimeError("获取额外任务失败：作品 ID 重复")
            seen.add(work_id)
        return [task for task in tasks if not task["completed"]], sum(task["completed"] for task in tasks)

    def _process_single_task(self, task: Dict, task_id: str) -> bool:
        ensure_score_date(self.score_date)
        self._report_listen(task["work"])
        signer = Signer(self.session, task_id, self.logger, self.config, self.score_date)
        return signer.sign(task["work"], is_extra=True)

    def _report_listen(self, work: Dict) -> None:
        ensure_score_date(self.score_date)
        csrf = self.session.cookies["__csrf"]
        data = {
            "workId": work["id"],
            "resourceId": work["resourceId"],
            "bizResourceId": "",
            "interactType": "PLAY_END",
            "csrf_token": csrf,
        }
        params = {
            "params": self.signer._get_params(data),
            "encSecKey": self.signer._get_enc_sec_key(),
        }
        response = self.session.post(
            url=f"{self.api['report_listen']}?csrf_token={csrf}",
            data=params,
            headers={"Referer": "https://mp.music.163.com/"},
            timeout=(10, 45),
        ).json()
        ensure_score_date(self.score_date)
        if not isinstance(response, dict) or response.get("code") != 200:
            raise RuntimeError("上报听歌记录失败：服务端未确认成功")
        self.logger.info(f"歌曲 {work['name']} 听歌记录上报成功")
