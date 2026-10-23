import json
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

from .config import Config
from .logger import Logger


class NotificationService:
    """通过 SMTP 发送邮件；配置 BREVO_API_KEY 后优先使用 HTTPS 邮件 API。"""

    def __init__(self, config: Config, logger: Logger):
        self.config = config
        self.logger = logger

    def send_notification(self, subject: str, content: str) -> bool:
        recipient = (self.config.get("notify_email") or "").strip()
        if not recipient:
            self.logger.warning("未配置 NOTIFY_EMAIL，通知未发送")
            return False

        api_key = self.config.get("brevo_api_key")
        if api_key:
            return self._send_brevo(api_key, recipient, subject, content)
        return self._send_smtp(recipient, subject, content)

    def _send_brevo(self, api_key: str, recipient: str, subject: str, content: str) -> bool:
        sender = (self.config.get("brevo_sender_email") or "").strip()
        if not sender:
            self.logger.error("已配置 BREVO_API_KEY，但缺少 BREVO_SENDER_EMAIL（需在 Brevo 验证发件人）")
            return False

        payload = {
            "sender": {"email": sender, "name": "NCMP Notification"},
            "to": [{"email": recipient}],
            "subject": subject,
            "textContent": content,
        }
        request = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status in (200, 201, 202):
                    self.logger.info("邮件 API 已接受通知请求（不等于收件箱已投递）")
                    return True
                self.logger.error(f"邮件 API 请求失败：HTTP {response.status}")
                return False
        except urllib.error.HTTPError as exc:
            # 不打印含有收件地址或认证信息的完整响应体
            self.logger.error(f"邮件 API 请求失败：HTTP {exc.code}，请核对发件人验证与 API Key")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.logger.error(f"邮件 API 连接失败：{type(exc).__name__}")
        return False

    def _send_smtp(self, recipient: str, subject: str, content: str) -> bool:
        password = self.config.get("email_password")
        if not password:
            self.logger.warning("未配置 EMAIL_PASSWORD（邮箱 SMTP 授权码），通知未发送")
            return False

        host = (self.config.get("smtp_server", "smtp.gmail.com") or "").strip()
        username = (self.config.get("smtp_username") or recipient).strip()
        sender = (self.config.get("smtp_from_email") or username).strip()
        try:
            configured_port = int(self.config.get("smtp_port", 465))
        except (TypeError, ValueError):
            self.logger.error("SMTP_PORT 不是有效端口号")
            return False
        if not host or not username or not sender:
            self.logger.error("SMTP_SERVER、SMTP_USERNAME 或 SMTP_FROM_EMAIL 配置不完整")
            return False

        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(content, charset="utf-8")

        if configured_port == 465:
            methods = [("SSL", 465), ("STARTTLS", 587)]
        elif configured_port in (587, 2525):
            methods = [("STARTTLS", configured_port), ("SSL", 465)]
        else:
            # 非标准端口以 STARTTLS 尝试，不擅自改用其他端口
            methods = [("STARTTLS", configured_port)]

        context = ssl.create_default_context()
        for mode, port in methods:
            try:
                self.logger.info(f"尝试通过 SMTP {mode} 端口 {port} 发送通知")
                if mode == "SSL":
                    with smtplib.SMTP_SSL(host, port, timeout=12, context=context) as server:
                        server.login(username, password)
                        server.send_message(message)
                else:
                    with smtplib.SMTP(host, port, timeout=12) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        server.ehlo()
                        server.login(username, password)
                        server.send_message(message)
                self.logger.info(f"SMTP {mode}:{port} 已成功提交邮件")
                return True
            except smtplib.SMTPAuthenticationError:
                self.logger.error("SMTP 身份验证失败：检查 SMTP 授权码和登录账号，停止重试")
                return False
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
                self.logger.error("SMTP 拒绝发件人或收件人，请检查邮箱地址及服务商权限")
                return False
            except (smtplib.SMTPException, socket.timeout, TimeoutError, OSError, ssl.SSLError) as exc:
                self.logger.warning(f"SMTP {mode}:{port} 失败：{type(exc).__name__}；将尝试其他已配置方式")

        self.logger.error(
            "邮件未发送：SMTP 465/587 可能被网络阻断、服务端主动断连或配置不匹配。"
            " 如 GitHub 托管 Runner 持续无法连接，可改用 HTTPS 邮件 API。"
        )
        return False
