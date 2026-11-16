import requests

from ..utils.config import Config
from ..utils.logger import Logger
from .signer import beijing_date, ensure_score_date
from .tasks.daily import DailyTask
from .tasks.extra import ExtraTask


class MusicPartnerBot:
    def __init__(self, config: Config, logger: Logger, session: requests.Session, score_date=None):
        self.config = config
        self.logger = logger
        self.session = session
        self.score_date = score_date or beijing_date()
        self.result = None
        self.api = {"user_info": "https://music.163.com/api/nuser/account/get"}

    def run(self) -> bool:
        try:
            ensure_score_date(self.score_date)
            self._verify_user()
            daily = DailyTask(self.session, self.logger, self.config, self.score_date)
            daily_result = daily.process_and_verify()
            extra = ExtraTask(self.session, self.logger, self.config, self.score_date)
            extra_result = extra.process_extra_tasks(daily_result["task_id"])
            ensure_score_date(self.score_date)
            self.result = {"daily": daily.result, "extra": extra_result}
            return True
        except Exception as error:
            self.logger.error(f"执行失败: {error}")
            return False

    def _verify_user(self) -> None:
        self.logger.info("开始验证用户信息...")
        response = self.session.get(url=self.api["user_info"], timeout=(10, 45)).json()
        profile = response.get("profile")
        if not profile:
            raise RuntimeError("获取用户信息失败")
        self.logger.info(f'用户名: {profile["nickname"]}')
