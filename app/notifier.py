import time
import hmac
import hashlib
import base64
import urllib.parse
import logging
from typing import Dict, Any, Optional
import httpx
from app.config import get_now_beijing, DEFAULT_CONFIG

logger = logging.getLogger("fnos.notify")

class Notifier:
    def __init__(self, db_session_factory=None):
        self.db_session_factory = db_session_factory

    async def get_config(self) -> Dict[str, Any]:
        if self.db_session_factory:
            from app.models import SystemConfig
            async with self.db_session_factory() as session:
                item = await session.get(SystemConfig, "notify")
                if item and isinstance(item.value, dict):
                    return item.value
        return DEFAULT_CONFIG["notify"]

    def _is_dnd_time(self, policy: Dict[str, Any]) -> bool:
        if not policy.get("dnd_enabled", False):
            return False
        start = int(policy.get("dnd_start", 23))
        end = int(policy.get("dnd_end", 8))
        cur_hour = get_now_beijing().hour

        if start > end:
            # 跨午夜：例如 23点到次日8点
            return cur_hour >= start or cur_hour < end
        else:
            return start <= cur_hour < end

    async def send(
        self,
        title: str,
        content: str,
        level: str = "info",  # info, success, fail, urgent
        channel: str = "all",
        override_cfg: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        cfg = override_cfg if override_cfg is not None else await self.get_config()
        policy = cfg.get("policy", {})

        # DND 免打扰校验 (紧急通知 urgent 可穿透免打扰)
        if level != "urgent" and self._is_dnd_time(policy):
            logger.info(f"[Notifier] DND Active. Suppressing notification: {title}")
            return {"status": "suppressed_by_dnd", "title": title}

        # 策略过滤：模式校验
        mode = policy.get("mode", "all")
        # 学术/资讯订阅 (digest) 属于核心数据速报，不受 fail_only (仅打卡失败告警) 限制
        if level not in ["digest", "fail", "urgent"]:
            if mode == "fail_only":
                return {"status": "filtered_by_mode", "mode": mode}
            if mode == "silent":
                return {"status": "filtered_by_mode", "mode": mode}
        if mode == "urgent_only" and level != "urgent":
            return {"status": "filtered_by_mode", "mode": mode}

        results = {}

        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            # 1. PushPlus
            if (channel in ["all", "pushplus"]) and cfg.get("pushplus", {}).get("enabled"):
                results["pushplus"] = await self._send_pushplus(client, cfg["pushplus"], title, content)

            # 2. 飞书 Webhook
            if (channel in ["all", "feishu"]) and cfg.get("feishu", {}).get("enabled"):
                results["feishu"] = await self._send_feishu(client, cfg["feishu"], title, content)

            # 3. 钉钉 Webhook
            if (channel in ["all", "dingtalk"]) and cfg.get("dingtalk", {}).get("enabled"):
                results["dingtalk"] = await self._send_dingtalk(client, cfg["dingtalk"], title, content)

            # 4. 企业微信 Webhook
            if (channel in ["all", "wechat_work"]) and cfg.get("wechat_work", {}).get("enabled"):
                results["wechat_work"] = await self._send_wechat_work(client, cfg["wechat_work"], title, content)

            # 5. Bark (iOS)
            if (channel in ["all", "bark"]) and cfg.get("bark", {}).get("enabled"):
                results["bark"] = await self._send_bark(client, cfg["bark"], title, content)

            # 6. 自定义 Webhook
            if (channel in ["all", "custom"]) and cfg.get("custom", {}).get("enabled"):
                results["custom"] = await self._send_custom(client, cfg["custom"], title, content)

        return results

    async def _send_pushplus(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        token = cfg.get("token", "").strip()
        if not token:
            return {"ok": False, "error": "Token 为空"}
        try:
            url = "http://www.pushplus.plus/send"
            is_md = any(m in content for m in ["## ", "### ", "- 🏛️", "- ⏳", "**", "`"])
            payload = {
                "token": token,
                "title": title[:100],
                "content": (content if is_md else content.replace("\n", "<br/>"))[:30000],
                "template": "markdown" if is_md else "html"
            }
            resp = await client.post(url, json=payload, timeout=15.0)
            data = resp.json() if resp.status_code == 200 else {}
            ok = resp.status_code == 200 and data.get("code") == 200
            msg = data.get("msg") or resp.text[:100]
            if not ok and resp.status_code == 200:
                logger.warning(f"[Notifier] PushPlus returned code {data.get('code')}: {msg}")
            return {"ok": ok, "status": resp.status_code, "msg": msg}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _send_feishu(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        webhook = cfg.get("webhook", "").strip()
        if not webhook:
            return {"ok": False, "error": "Webhook 为空"}
        try:
            is_md = any(m in content for m in ["## ", "### ", "- 🏛️", "- ⏳", "**", "`"])
            if is_md:
                payload = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {"tag": "plain_text", "content": title},
                            "template": "blue"
                        },
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": content
                            }
                        ]
                    }
                }
            else:
                payload = {
                    "msg_type": "text",
                    "content": {"text": f"{title}\n\n{content}"}
                }
            resp = await client.post(webhook, json=payload)
            return {"ok": resp.status_code == 200, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _send_dingtalk(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        webhook = cfg.get("webhook", "").strip()
        secret = cfg.get("secret", "").strip()
        if not webhook:
            return {"ok": False, "error": "Webhook 为空"}

        url = webhook
        if secret:
            timestamp = str(round(time.time() * 1000))
            secret_enc = secret.encode("utf-8")
            string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
            hmac_code = hmac.new(secret_enc, string_to_sign, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            sep = "&" if "?" in webhook else "?"
            url = f"{webhook}{sep}timestamp={timestamp}&sign={sign}"

        try:
            is_md = any(m in content for m in ["## ", "### ", "- 🏛️", "- ⏳", "**", "`"])
            if is_md:
                payload = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": title,
                        "text": f"### {title}\n\n{content}"
                    }
                }
            else:
                payload = {
                    "msgtype": "text",
                    "text": {"content": f"{title}\n\n{content}"}
                }
            resp = await client.post(url, json=payload)
            return {"ok": resp.status_code == 200, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _send_wechat_work(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        webhook = cfg.get("webhook", "").strip()
        if not webhook:
            return {"ok": False, "error": "Webhook 为空"}
        try:
            is_md = any(m in content for m in ["## ", "### ", "- 🏛️", "- ⏳", "**", "`"])
            if is_md:
                payload = {
                    "msgtype": "markdown",
                    "markdown": {
                        "content": f"### {title}\n\n{content[:4000]}"
                    }
                }
            else:
                payload = {
                    "msgtype": "text",
                    "text": {"content": f"{title}\n\n{content}"}
                }
            resp = await client.post(webhook, json=payload)
            return {"ok": resp.status_code == 200, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _send_bark(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        server = cfg.get("server_url", "https://api.day.app").rstrip("/")
        key = cfg.get("device_key", "").strip()
        if not key:
            return {"ok": False, "error": "Device Key 为空"}
        try:
            url = f"{server}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(content)}"
            resp = await client.get(url)
            return {"ok": resp.status_code == 200, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _send_custom(self, client: httpx.AsyncClient, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        url = cfg.get("url", "").strip()
        if not url:
            return {"ok": False, "error": "URL 为空"}
        method = cfg.get("method", "POST").upper()
        headers = cfg.get("headers", {})
        tpl = cfg.get("template", '{"title": "{title}", "content": "{content}"}')

        body = tpl.replace("{title}", title).replace("{content}", content.replace("\n", "\\n").replace('"', '\\"'))
        try:
            resp = await client.request(method, url, headers=headers, content=body.encode("utf-8"))
            return {"ok": resp.status_code == 200, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
