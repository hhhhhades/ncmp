import base64
import codecs
import json
import random
import re
import string
import time
from typing import Tuple
from datetime import datetime, timedelta, timezone

import requests
from Crypto.Cipher import AES

from ..utils.config import Config
from ..utils.logger import Logger


def beijing_date():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def ensure_score_date(score_date):
    if beijing_date() != score_date:
        raise RuntimeError("北京时间日期已改变，停止本次任务，不能跨日继续评分")


class Signer:
    def __init__(self, session: requests.Session, task_id: str, logger: Logger, config: Config, score_date=None):
        self.session = session
        self.task_id = task_id
        self.logger = logger
        self.config = config
        self.score_date = score_date or beijing_date()
        self.sign_url = "https://interface.music.163.com/weapi/music/partner/work/evaluate"

        # 加密相关常量
        self.random_str = self._generate_random_string(16)
        self.pub_key = "010001"
        self.modulus = "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
        self.iv = "0102030405060708"
        self.aes_key = "0CoJUm6Qyw8W8jud"

        self.name_pattern = re.compile('.*[a-zA-Z].*')

    def _generate_random_string(self, length: int) -> str:
        """生成指定长度的随机字符串"""
        return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

    def _add_to_16(self, text: str) -> bytes:
        """将字符串补充到16的倍数"""
        pad = 16 - len(text) % 16
        text = text + chr(pad) * pad
        return text.encode('utf-8')

    def _aes_encrypt(self, text: str, key: str) -> str:
        """AES加密"""
        encryptor = AES.new(key.encode('utf-8'), AES.MODE_CBC, self.iv.encode('utf-8'))
        encrypt_text = encryptor.encrypt(self._add_to_16(text))
        return base64.b64encode(encrypt_text).decode('utf-8')

    def _get_params(self, data: dict) -> str:
        """获取加密后的参数"""
        text = json.dumps(data)
        params = self._aes_encrypt(text, self.aes_key)
        params = self._aes_encrypt(params, self.random_str)
        return params

    def _get_enc_sec_key(self) -> str:
        """获取加密密钥"""
        text = self.random_str[::-1]
        rs = int(codecs.encode(text.encode('utf-8'), 'hex_codec'), 16)
        rs = pow(rs, int(self.pub_key, 16), int(self.modulus, 16))
        return format(rs, 'x').zfill(256)

    def _get_score_and_tag(self, work: dict) -> Tuple[str, str]:
        """根据作品信息获取评分和标签"""
        # 获取评分策略，默认为4（3-4分）
        score_strategy = int(self.config.get("score", 3))

        # 检查名称中是否包含英文
        has_english = bool(self.name_pattern.match(work["name"] + work["authorName"]))

        # 根据策略和名称决定评分
        if score_strategy == 1:  # 1-2分策略
            score = "2" if has_english else "1"
        elif score_strategy == 2:  # 2-3分策略
            score = "3" if has_english else "2"
        elif score_strategy == 3:  # 3-4分策略（默认）
            score = "4" if has_english else "3"
        else:  # 固定4分
            score = "4"

        return score, f"{score}-A-1"

    def sign(self, work: dict, is_extra: bool = False) -> bool:
        """Return True only for an acknowledged score; never retry uncertain POSTs."""
        try:
            for attempt in range(3):
                ensure_score_date(self.score_date)
                delay = self.config.get_wait_time()
                self.logger.info(f"等待 {delay:.1f} 秒后继续...")
                time.sleep(delay)
                ensure_score_date(self.score_date)
                csrf = str(self.session.cookies["__csrf"])
                score, tag = self._get_score_and_tag(work)
                data = {
                    "taskId": self.task_id,
                    "workId": work["id"],
                    "score": score,
                    "tags": tag,
                    "customTags": "%5B%5D",
                    "comment": "",
                    "syncYunCircle": "true",
                    "csrf_token": csrf,
                }
                if is_extra:
                    data["extraResource"] = "true"
                params = {
                    "params": self._get_params(data),
                    "encSecKey": self._get_enc_sec_key(),
                }
                ensure_score_date(self.score_date)
                response = self.session.post(
                    url=f"{self.sign_url}?csrf_token={csrf}",
                    data=params,
                    timeout=(10, 45),
                ).json()
                ensure_score_date(self.score_date)
                if not isinstance(response, dict):
                    raise RuntimeError("评分响应无效")
                if response.get("code") == 200:
                    self.logger.info(f'{work["name"]}「{work["authorName"]}」评分完成：{score}分')
                    return True
                message = response.get("message") or response.get("msg") or "未知错误"
                if response.get("code") == 405 and "资源状态异常" in message:
                    self.logger.warning(f'歌曲「{work["name"]}」资源状态异常，跳过，不计入成功')
                    return False
                if "频繁" in message and attempt < 2:
                    self.logger.info("服务端拒绝频繁评分，按配置等待后重试")
                    continue
                raise RuntimeError(f"评分未完成（响应码: {response.get('code')}）")
            return False
        except requests.RequestException as error:
            self.logger.error(f"评分请求未确认（{type(error).__name__}），不自动重发")
            raise RuntimeError("评分请求未确认，需重新读取服务端状态") from None
        except Exception as error:
            self.logger.error(f'歌曲「{work["name"]}」评分异常：{error}')
            raise RuntimeError(f"评分过程出错: {error}") from None
