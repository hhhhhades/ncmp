import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# 直接运行 tests/test_notification.py 时将仓库根目录加入模块路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.config import Config
from src.utils.logger import Logger
from src.utils.notification import NotificationService


def main():
    # 邮件独立测试不要求网易云 Cookie，也不会刷新账号。
    config = Config.__new__(Config)
    config.config_data = {}
    names = {
        "NOTIFY_EMAIL": "notify_email",
        "EMAIL_PASSWORD": "email_password",
        "SMTP_SERVER": "smtp_server",
        "SMTP_PORT": "smtp_port",
        "SMTP_USERNAME": "smtp_username",
        "SMTP_FROM_EMAIL": "smtp_from_email",
        "BREVO_API_KEY": "brevo_api_key",
        "BREVO_SENDER_EMAIL": "brevo_sender_email",
    }
    for env_name, config_name in names.items():
        if value := os.getenv(env_name):
            config.config_data[config_name] = value

    config.config_data.setdefault("smtp_server", "smtp.gmail.com")
    config.config_data.setdefault("smtp_port", 465)

    logger = Logger(log_level=logging.INFO)
    notifier = NotificationService(config, logger)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = "[NCMP] 邮件通知测试"
    body = "这是一封 NCMP GitHub Actions 邮件通知测试。\nUTC 时间：" + now
    logger.info("准备发送一封测试邮件（不会刷新 Cookie 或运行评分）")
    success = notifier.send_notification(subject, body)
    if not success:
        logger.error("❌ 测试邮件发送失败，请按日志提示核对配置")
        sys.exit(1)
    logger.info("✅ 测试邮件已提交至发送服务，请检查收件箱及垃圾邮件")
    sys.exit(0)


if __name__ == "__main__":
    main()
