import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.core.bot import MusicPartnerBot
from src.core.signer import beijing_date, ensure_score_date
from src.utils.config import Config
from src.utils.logger import Logger
from src.utils.notification import NotificationService
from src.validators.cookie import CookieValidator


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 45))
        try:
            return super().request(method, url, **kwargs)
        except requests.RequestException as error:
            # Request exception strings can include query-string cookies; never log them.
            raise RuntimeError(f"网络请求未确认（{type(error).__name__}），停止本次执行") from None


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    logger = Logger()
    notifier = None
    result_path = os.getenv("NCMP_RESULT_FILE")
    started_at = utc_now()
    score_date = os.getenv("NCMP_SCORE_DATE") or beijing_date()
    try:
        if result_path:
            Path(result_path).unlink(missing_ok=True)
        ensure_score_date(score_date)
        config = Config()
        notifier = NotificationService(config, logger)
        with TimeoutSession() as session:
            session.cookies.set("MUSIC_U", config.get("Cookie_MUSIC_U"))
            session.cookies.set("__csrf", config.get("Cookie___csrf"))
            validator = CookieValidator(session, logger)
            is_valid, message = validator.validate()
            if not is_valid:
                logger.error(message)
                notifier.send_notification("网易云音乐合伙人 - Cookie失效提醒", f"请更新Cookie\n详细信息: {message}")
                return 1
            ensure_score_date(score_date)
            bot = MusicPartnerBot(config, logger, session, score_date)
            if not bot.run():
                logger.end("❌ 执行失败", True)
                notifier.send_notification("网易云音乐合伙人 - 执行失败提醒", "程序执行失败，请检查日志")
                return 1
            ensure_score_date(score_date)
            if not bot.result:
                raise RuntimeError("缺少经服务端核验的评分结果")
            receipt = {
                "schema": 1,
                "status": "completed",
                "started_at": started_at,
                "completed_at": utc_now(),
                "score_date": score_date,
                **bot.result,
            }
            if result_path:
                destination = Path(result_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, destination)
            logger.end("✅ 执行成功", False)
            return 0
    except Exception as error:
        logger.error(f"程序异常: {error}")
        logger.end("❌ 执行失败", True)
        if notifier is not None:
            try:
                notifier.send_notification("网易云音乐合伙人 - 异常提醒", "程序执行异常，请检查 GitHub Actions 日志")
            except Exception:
                logger.error("发送异常通知失败，请检查邮件配置")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
