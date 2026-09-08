import os
import json
import re
import time
import base64
import logging
from typing import Tuple, Optional, Dict, Any, List
from datetime import datetime
import httpx
from app.config import DATA_DIR
from app.security import decrypt_credential
from app.captcha import solve_page_captcha

logger = logging.getLogger("fnos.crawler")

# 持久化会话目录
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

# 常用浏览器 User-Agent
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def get_session_path(task_id: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{task_id}_state.json")

def has_valid_session(task_id: str) -> bool:
    p = get_session_path(task_id)
    return os.path.exists(p) and os.path.getsize(p) > 10

def get_session_updated_at(task_id: str) -> Optional[str]:
    p = get_session_path(task_id)
    if os.path.exists(p):
        mtime = os.path.getmtime(p)
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return None

def decode_b64_json(s: str) -> Optional[Dict[str, Any]]:
    try:
        s = s.replace("-", "+").replace("_", "/")
        s += "=" * ((4 - len(s) % 4) % 4)
        raw = base64.b64decode(s).decode("utf-8", errors="ignore")
        return json.loads(raw)
    except Exception:
        return None

def parse_jwt_exp(token_str: str) -> Optional[int]:
    try:
        start = token_str.find("eyJ")
        if start == -1:
            return None
        sub = token_str[start:].split(";")[0].split("&")[0].split(" ")[0].strip("\"'")
        parts = sub.split(".")
        data = None
        if len(parts) >= 2:
            data = decode_b64_json(parts[1])
        if not data and len(parts) >= 1:
            data = decode_b64_json(parts[0])

        if not isinstance(data, dict):
            return None

        for k in ["exp", "expires_at"]:
            if k in data and isinstance(data[k], (int, float)):
                return int(data[k])
        for k in ["ts", "iat"]:
            if k in data and isinstance(data[k], (int, float)):
                return int(data[k]) + 30 * 86400
        return None
    except Exception:
        return None

def get_cookie_expiration_days(headers: Dict[str, Any]) -> Tuple[bool, Optional[float], Optional[int], str]:
    if not isinstance(headers, dict):
        return False, None, None, ""
    for k, v in headers.items():
        if not isinstance(v, str):
            continue
        if "eyJ" in v:
            exp = parse_jwt_exp(v)
            if exp:
                now_unix = time.time()
                diff_sec = exp - now_unix
                days = round(diff_sec / 86400.0, 1)

                t = time.localtime(exp + 8 * 3600)
                exp_date_str = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
                return True, days, exp, exp_date_str
    return False, None, None, ""

def extract_reward(result_msg: str, response_text: str) -> Tuple[Optional[int], Optional[str]]:
    combined = (result_msg + " " + response_text).strip()
    try:
        m = re.search(r'(?:获得|奖励|赠送|领到|增加|领取)\s*(\d+)\s*(?:个|颗|点|枚)?\s*(鸡腿|京豆|积分|经验|米粒|金币|铜币|收益)?', combined)
        if m:
            val = int(m.group(1))
            unit = m.group(2) if m.group(2) else ""
            return val, unit

        m2 = re.search(r'(\d+)\s*(?:个|颗|点|枚)\s*(鸡腿|京豆|积分|经验|米粒|金币|铜币)', combined)
        if m2:
            return int(m2.group(1)), m2.group(2)
    except Exception:
        pass

    units = ["鸡腿", "京豆", "金币", "积分", "经验", "米粒", "铜币", "点数"]
    for u in units:
        if u in combined:
            try:
                m = re.search(r'(\d+)\s*(?:个|颗|点|枚)?\s*' + u, combined)
                if m:
                    return int(m.group(1)), u
            except Exception:
                pass

    try:
        data = json.loads(response_text)
        if isinstance(data, dict):
            for k in ["gain", "reward", "points", "beans"]:
                if k in data and isinstance(data[k], (int, float)):
                    return int(data[k]), "点"
    except Exception:
        pass

    return None, None

def clean_html_noise(html_text: str, max_bytes: int = 50 * 1024 * 1024) -> str:
    if not html_text:
        return ""

    text = html_text
    noise_patterns = [
        r"(?is)<script.*?>.*?</script>",
        r"(?is)<style.*?>.*?</style>",
        r"(?is)<svg.*?>.*?</svg>",
        r"(?is)<head.*?>.*?</head>",
        r"(?is)<noscript.*?>.*?</noscript>",
        r"(?is)<iframe.*?>.*?</iframe>",
        r"(?is)<!--.*?-->",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)

    lines = []
    for line in text.splitlines():
        line_s = " ".join(line.split())
        if line_s:
            lines.append(line_s)

    cleaned = "\n".join(lines)
    if max_bytes > 0:
        encoded = cleaned.encode("utf-8")
        if len(encoded) > max_bytes:
            encoded = encoded[:max_bytes]
            cleaned = encoded.decode("utf-8", errors="ignore")
    return cleaned


class CrawlerResult:
    def __init__(
        self,
        ok: bool,
        status_code: int,
        text: str,
        raw_html: str = "",
        headers: Dict[str, str] = None,
        set_cookies: List[str] = None,
        latency_ms: int = 0,
        mode_used: str = "httpx",
        error: str = ""
    ):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.raw_html = raw_html
        self.headers = headers or {}
        self.set_cookies = set_cookies or []
        self.latency_ms = latency_ms
        self.mode_used = mode_used
        self.error = error

class SmartCrawler:
    """
    飞牛 NAS 专属智能双模爬虫引擎：
    - 一级：高性能异步 HTTP (httpx)，极速抓取 API 与静态页面；
    - 二级：无头 Chromium 智能升阶 (Playwright)，攻克 ScienceDirect、React/Vue 单页面 SPA 与复杂反爬；
    - 会话持久化：原生集成 Playwright StorageState，免去手动抓 Cookie。
    """
    def __init__(self):
        self.playwright_available = False
        self._check_playwright()

    def _check_playwright(self):
        try:
            import playwright  # noqa: F401
            self.playwright_available = True
        except ImportError:
            self.playwright_available = False
            logger.warning("Playwright 未安装，智能无头浏览器升阶模式暂不可用")

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        timeout: int = 30,
        max_bytes: int = 50 * 1024 * 1024,
        force_headless: bool = False,
        session_path: Optional[str] = None
    ) -> CrawlerResult:
        t_start = time.time()
        req_headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        if headers:
            req_headers.update(headers)

        # 若存在持久化会话文件，且使用的是 HTTPX 模式，注入会话中的 Cookies
        if session_path and os.path.exists(session_path) and "Cookie" not in req_headers and "cookie" not in req_headers:
            try:
                with open(session_path, "r", encoding="utf-8") as sf:
                    s_data = json.load(sf)
                    cookie_parts = []
                    for c in s_data.get("cookies", []):
                        if "name" in c and "value" in c:
                            cookie_parts.append(f"{c['name']}={c['value']}")
                    if cookie_parts:
                        req_headers["Cookie"] = "; ".join(cookie_parts)
            except Exception as e:
                logger.warning(f"[Crawler] Error reading cookies from session file {session_path}: {e}")

        # 若强制使用无头浏览器或指定了 session_path
        if force_headless:
            logger.info(f"[Crawler] Task requested force_headless=True for {url}")
            return await self._fetch_with_playwright(url, req_headers, timeout, max_bytes, session_path=session_path)

        # 一级抓取：使用异步 httpx
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=10,
                timeout=float(timeout),
                verify=False
            ) as client:
                resp = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=req_headers,
                    content=body.encode("utf-8") if body else None
                )
                latency_ms = int((time.time() - t_start) * 1000)
                set_cookies = resp.headers.get_list("set-cookie")
                raw_text = resp.text

                # 判断是否需要升阶到无头 Chromium
                needs_escalation, reason = self._check_needs_escalation(resp.status_code, raw_text, url)
                if needs_escalation and self.playwright_available and method.upper() == "GET":
                    logger.info(f"[Crawler] Triggering Smart Escalation to Headless Chromium for {url}: {reason}")
                    playwright_res = await self._fetch_with_playwright(url, req_headers, timeout, max_bytes, session_path=session_path)
                    if playwright_res.ok:
                        return playwright_res
                    logger.warning(f"[Crawler] Playwright escalation failed ({playwright_res.error}), falling back to httpx response")

                clean_text = clean_html_noise(raw_text, max_bytes) if "html" in resp.headers.get("content-type", "").lower() else (raw_text[:max_bytes] if max_bytes > 0 else raw_text)

                return CrawlerResult(
                    ok=(200 <= resp.status_code < 400),
                    status_code=resp.status_code,
                    text=clean_text,
                    raw_html=raw_text,
                    headers=dict(resp.headers),
                    set_cookies=set_cookies,
                    latency_ms=latency_ms,
                    mode_used="httpx"
                )
        except Exception as ex:
            logger.warning(f"[Crawler] httpx fetch error for {url}: {ex}")
            if self.playwright_available and method.upper() == "GET":
                logger.info("[Crawler] Escalating to Playwright due to httpx network exception...")
                return await self._fetch_with_playwright(url, req_headers, timeout, max_bytes, session_path=session_path)
            latency_ms = int((time.time() - t_start) * 1000)
            return CrawlerResult(
                ok=False,
                status_code=0,
                text="",
                latency_ms=latency_ms,
                mode_used="httpx",
                error=str(ex)
            )

    def _check_needs_escalation(self, status_code: int, text: str, url: str) -> Tuple[bool, str]:
        text_lower = text.lower()
        if "just a moment..." in text_lower or "cf-turnstile" in text_lower or "challenge-running" in text_lower:
            return True, "检测到 Cloudflare WAF 人机验证挑战"

        if status_code in [302, 403] and ("sciencedirect" in url.lower() or "elsevier" in url.lower() or "cloudflare" in text_lower):
            return True, f"HTTP {status_code} 拦截，命中学术/防护站点反爬策略"

        if status_code == 200:
            cleaned = clean_html_noise(text, max_bytes=1000)
            if len(cleaned.strip()) < 150:
                if any(spa_tag in text_lower for spa_tag in ['<div id="root">', '<div id="app">', 'you need to enable javascript', 'javascript is required']):
                    return True, "页面为客户端 JavaScript/React 渲染的动态 SPA 空壳"

        return False, ""

    async def _launch_browser(self, p):
        """
        启动无头浏览器：优先尝试官方 Google Chrome (channel='chrome')，
        若系统未安装官方 Chrome 则平滑回退至纯净 Google Chromium 内核
        """
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled"
        ]
        try:
            return await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=launch_args
            )
        except Exception:
            pass

        return await p.chromium.launch(
            headless=True,
            args=launch_args
        )

    async def _fetch_with_playwright(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: int = 30,
        max_bytes: int = 50 * 1024 * 1024,
        session_path: Optional[str] = None

    ) -> CrawlerResult:
        if not self.playwright_available:
            return CrawlerResult(
                ok=False,
                status_code=0,
                text="",
                error="Playwright 未安装或环境不可用",
                mode_used="playwright"
            )

        t_start = time.time()
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await self._launch_browser(p)

                # 如果存在有效会话文件，加载 StorageState
                context_kwargs = {
                    "user_agent": headers.get("User-Agent", DEFAULT_UA),
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "zh-CN"
                }
                if session_path and os.path.exists(session_path) and os.path.getsize(session_path) > 10:
                    context_kwargs["storage_state"] = session_path
                    logger.info(f"[Crawler] Injected persistent session StorageState: {session_path}")

                context = await browser.new_context(**context_kwargs)

                # 设置自定义 Cookie (如果有)
                cookie_header = headers.get("Cookie") or headers.get("cookie")
                if cookie_header:
                    try:
                        cookies_to_add = []
                        domain = url.split("//")[-1].split("/")[0].split(":")[0]
                        for part in cookie_header.split(";"):
                            if "=" in part:
                                k, v = part.strip().split("=", 1)
                                cookies_to_add.append({"name": k.strip(), "value": v.strip(), "domain": domain, "path": "/"})
                        if cookies_to_add:
                            await context.add_cookies(cookies_to_add)
                    except Exception as ce:
                        logger.warning(f"[Crawler] Error adding cookies: {ce}")

                page = await context.new_page()
                # 增强防检测脚本
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                """)

                response = None
                try:
                    response = await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                except Exception:
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception as e:
                        logger.warning(f"[Crawler] Playwright page.goto warning: {e}")

                await page.wait_for_timeout(2500)

                raw_html = await page.content()
                inner_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                status_code = response.status if response else 200

                # 若提供了 session_path，更新持久化状态
                if session_path:
                    try:
                        await context.storage_state(path=session_path)
                    except Exception as se:
                        logger.warning(f"[Crawler] Failed to update storage_state: {se}")

                await browser.close()

                latency_ms = int((time.time() - t_start) * 1000)
                final_text = inner_text if len(inner_text.strip()) > 100 else clean_html_noise(raw_html, max_bytes)
                if max_bytes > 0:
                    final_text = final_text[:max_bytes]


                return CrawlerResult(
                    ok=(200 <= status_code < 400),
                    status_code=status_code,
                    text=final_text,
                    raw_html=raw_html,
                    latency_ms=latency_ms,
                    mode_used="playwright"
                )
        except Exception as e:
            latency_ms = int((time.time() - t_start) * 1000)
            logger.error(f"[Crawler] Playwright execution error for {url}: {e}")
            return CrawlerResult(
                ok=False,
                status_code=0,
                text="",
                latency_ms=latency_ms,
                mode_used="playwright",
                error=f"Playwright 渲染异常: {str(e)}"
            )

    async def perform_auto_login(self, task_id: str, account_cfg: dict, llm_client=None) -> Tuple[bool, str]:
        """
        Playwright 自动账密登录与 StorageState 会话持久化向导
        """
        if not self.playwright_available:
            return False, "Playwright 无头浏览器环境未就绪"

        login_url = (account_cfg.get("login_url") or "").strip()
        username = (account_cfg.get("username") or "").strip()
        raw_pwd = account_cfg.get("password") or ""
        password = decrypt_credential(raw_pwd)

        if not login_url:
            return False, "未配置登录页面地址 (login_url)"
        if not username or not password:
            return False, "未配置登录账号或密码"

        user_selector = account_cfg.get("user_selector") or 'input[name="username"], input[name="user"], input[name="login"], input[name="email"], input[id*="user"], input[type="text"]'
        pwd_selector = account_cfg.get("pwd_selector") or 'input[type="password"]'
        submit_selector = account_cfg.get("submit_selector") or 'button[type="submit"], input[type="submit"], button:has-text("登录"), button:has-text("Sign in"), #submit, .login-btn'
        captcha_img_selector = account_cfg.get("captcha_img_selector")
        captcha_input_selector = account_cfg.get("captcha_input_selector") or 'input[name*="captcha"], input[name*="code"], input[id*="captcha"]'
        wait_selector = account_cfg.get("wait_selector")

        session_path = get_session_path(task_id)

        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await self._launch_browser(p)
                context = await browser.new_context(
                    user_agent=DEFAULT_UA,
                    viewport={"width": 1280, "height": 800},
                    locale="zh-CN"
                )
                page = await context.new_page()
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                """)

                logger.info(f"[AutoLogin] Navigating to login page: {login_url}")
                await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # 1. 查找并填入用户名
                try:
                    u_el = await page.wait_for_selector(user_selector, timeout=8000)
                    await u_el.fill(username)
                    logger.info("[AutoLogin] Filled username")
                except Exception as ue:
                    await browser.close()
                    return False, f"未找到用户名输入框 ({user_selector}): {str(ue)}"

                # 2. 查找并填入密码
                try:
                    p_el = await page.wait_for_selector(pwd_selector, timeout=5000)
                    await p_el.fill(password)
                    logger.info("[AutoLogin] Filled password")
                except Exception as pe:
                    await browser.close()
                    return False, f"未找到密码输入框 ({pwd_selector}): {str(pe)}"

                # 3. 处理图形验证码 (若配置了选择器)
                if captcha_img_selector:
                    try:
                        logger.info(f"[AutoLogin] Detecting captcha using selector: {captcha_img_selector}")
                        code = await solve_page_captcha(page, captcha_img_selector, llm_client=llm_client)
                        if code:
                            c_in = await page.wait_for_selector(captcha_input_selector, timeout=5000)
                            await c_in.fill(code)
                            logger.info(f"[AutoLogin] Filled solved captcha code: {code}")
                        else:
                            logger.warning("[AutoLogin] Captcha could not be solved")
                    except Exception as ce:
                        logger.warning(f"[AutoLogin] Captcha processing error: {ce}")

                # 4. 点击提交登录按钮
                try:
                    s_btn = await page.wait_for_selector(submit_selector, timeout=5000)
                    await s_btn.click()
                    logger.info("[AutoLogin] Clicked submit button")
                except Exception as se:
                    await browser.close()
                    return False, f"未找到登录提交按钮 ({submit_selector}): {str(se)}"

                # 5. 等待页面跳转或成功标记
                try:
                    if wait_selector:
                        await page.wait_for_selector(wait_selector, timeout=15000)
                    else:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    # 容错：稍微再等 3 秒
                    await page.wait_for_timeout(3000)

                # 6. 提取并持久化 StorageState
                await context.storage_state(path=session_path)
                logger.info(f"[AutoLogin] Successfully generated StorageState at {session_path}")

                await browser.close()
                return True, "账号密码模拟登录成功，会话 (StorageState) 已成功持久化落盘！"
        except Exception as e:
            logger.error(f"[AutoLogin] Auto login failed for task {task_id}: {e}")
            return False, f"自动登录执行异常: {str(e)}"

    async def capture_page_screenshot(self, url: str, session_path: Optional[str] = None) -> bytes:
        """
        截取指定网页的实时快照 (供控制台二维码扫码交互使用)
        """
        if not self.playwright_available:
            return b""

        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await self._launch_browser(p)
                kwargs = {"viewport": {"width": 1024, "height": 768}}
                if session_path and os.path.exists(session_path):
                    kwargs["storage_state"] = session_path
                context = await browser.new_context(**kwargs)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)
                img = await page.screenshot(type="png")
                await browser.close()
                return img
        except Exception as e:
            logger.error(f"[Crawler] Screenshot failed: {e}")
            return b""

# 全局爬虫单例
crawler = SmartCrawler()
