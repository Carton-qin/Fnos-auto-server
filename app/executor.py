import json
import time
import asyncio
import logging
from typing import Tuple, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy import select
from app.config import format_now, get_now_beijing
from app.crawler import crawler, extract_reward, get_cookie_expiration_days, clean_html_noise
from app.models import Task, TaskLog, Reward

logger = logging.getLogger("fnos.executor")

class TaskExecutor:
    def __init__(self, db_session_factory, llm_client, notifier):
        self.db_session_factory = db_session_factory
        self.llm_client = llm_client
        self.notifier = notifier

    async def run_task(self, task_id: str, jitter_info: str = "") -> Tuple[bool, str, str]:
        async with self.db_session_factory() as session:
            task = await session.get(Task, task_id)
            if not task:
                logger.error(f"[Executor] Task {task_id} not found in database")
                return False, "任务不存在", "Task not found"

            task_name = task.name
            ttype = task.type or "checkin"
            retries = task.retry_count or 1
            retry_delay = task.retry_delay or 3

            logger.info(f"[Executor] Starting task: {task_name} (ID: {task_id}, Type: {ttype})")

            # 熔断校验
            stats = dict(task.stats or {})
            if stats.get("circuit_tripped"):
                tripped_until = stats.get("circuit_tripped_until", 0)
                if time.time() < tripped_until:
                    rem_min = int((tripped_until - time.time()) / 60)
                    msg = f"🛡️ 防封熔断中：任务连续失败触发保护，已跳过执行（剩余挂起 {rem_min} 分钟）"
                    logger.warning(f"[Executor] {task_name} {msg}")
                    return False, msg, "Circuit breaker tripped"

            success = False
            result_msg = ""
            error_details = ""
            latency_ms = 0

            for attempt in range(retries + 1):
                if attempt > 0:
                    logger.info(f"[Executor] Retrying task {task_name} (Attempt {attempt}/{retries})...")
                    await asyncio.sleep(retry_delay)

                t_start = time.time()
                try:
                    if ttype in ["checkin", "custom_http"]:
                        success, result_msg, error_details = await self._run_http_task(task)
                    elif ttype == "ai_digest":
                        success, result_msg, error_details = await self._run_ai_digest_task(task)
                    elif ttype == "uptime":
                        success, result_msg, error_details = await self._run_uptime_task(task)
                    else:
                        success, result_msg, error_details = False, f"未知任务类型: {ttype}", "Unknown task type"
                except Exception as ex:
                    success = False
                    error_details = f"Exception: {str(ex)}"
                    result_msg = f"执行异常: {str(ex)}"
                latency_ms = int((time.time() - t_start) * 1000)

                if success:
                    break

            now_str = format_now()
            today_str = format_now("%Y-%m-%d")

            # 更新任务执行状态
            task.last_run = now_str
            task.last_status = "success" if success else "fail"
            task.last_result = result_msg

            # 熔断与打卡统计更新
            circuit_tripped_just_now = False
            if success:
                stats["consecutive_fails"] = 0
                stats["circuit_tripped"] = False
                stats["circuit_tripped_until"] = 0
            else:
                fails = stats.get("consecutive_fails", 0) + 1
                stats["consecutive_fails"] = fails
                if fails >= 3 and not stats.get("circuit_tripped"):
                    stats["circuit_tripped"] = True
                    stats["circuit_tripped_until"] = time.time() + 6 * 3600
                    circuit_tripped_just_now = True
                    log_entry = TaskLog(
                        task_id=task_id,
                        task_name=task_name,
                        category="system",
                        status="warning",
                        message="🛡️ 防封熔断触发: 连续失败 3 次，保护性暂停定时调度 6 小时",
                        time=now_str
                    )
                    session.add(log_entry)

            # 打卡类型特有战绩统计与奖励提取
            reward_record = None
            if ttype == "checkin":
                reward_record = self._update_checkin_stats(task, stats, success, result_msg, error_details, today_str)

            task.stats = stats

            # AI 智能诊断
            ai_diag = ""
            if not success:
                if task.ai_diagnose:
                    logger.info(f"[Executor] Invoking LLM diagnosis for failed task: {task_name}")
                    try:
                        ai_diag = await self.llm_client.diagnose_failure(task_name, error_details)
                    except Exception as e:
                        ai_diag = f"诊断生成异常: {str(e)}"

                # 发送失败告警
                if task.notify_on_failure:
                    notify_content = f"任务名称：{task_name}\n执行状态：❌ 失败\n失败信息：{result_msg}\n"
                    if circuit_tripped_just_now:
                        notify_content += "\n🛡️ **防封熔断保护已触发**：任务已连续失败 3 次！已自动暂停该任务定时调度 6 小时。\n"
                    if jitter_info:
                        notify_content += f"防封延迟：已随机推迟 {jitter_info}\n"
                    if ai_diag:
                        notify_content += f"\n🤖 **AI 诊断建议**：\n{ai_diag}"
                    await self.notifier.send(f"【任务失败告警】{task_name}", notify_content, level="fail")
            else:
                # 发送成功通知
                if task.notify_on_success or ttype == "ai_digest":
                    notify_content = f"任务名称：{task_name}\n执行状态：✅ 成功\n\n{result_msg}"
                    if ttype == "checkin":
                        streak = stats.get("streak", 1)
                        tot_s = stats.get("total_success", 1)
                        tot_r = stats.get("total_reward", 0)
                        r_unit = stats.get("reward_unit", "")
                        notify_content += f"\n\n🔥 **连续打卡**：{streak} 天\n🎯 **累计达标**：{tot_s} 次"
                        if tot_r > 0 and r_unit:
                            notify_content += f"\n🎁 **累计斩获**：{tot_r} {r_unit}"
                    if jitter_info:
                        notify_content += f"\n🎲 **防封随机延迟**：已随机推迟 {jitter_info} 执行"
                    await self.notifier.send(f"【任务执行结果】{task_name}", notify_content, level="success")

            # 凭据 Cookie / JWT 到期主动预警 (剩余 <= 3 天)
            await self._check_cookie_expiration_warning(task, session, now_str)

            # 记录执行审计日志
            log_msg = result_msg + (f" (防封延迟: {jitter_info})" if jitter_info else "")
            task_log = TaskLog(
                task_id=task_id,
                task_name=task_name,
                category="task",
                status="success" if success else "fail",
                message=log_msg,
                ai_diag=ai_diag if ai_diag else None,
                latency_ms=latency_ms,
                time=now_str
            )
            session.add(task_log)
            if reward_record:
                session.add(reward_record)

            await session.commit()
            return success, result_msg, ai_diag

    def _update_checkin_stats(
        self,
        task: Task,
        stats: Dict[str, Any],
        success: bool,
        result_msg: str,
        response_text: str,
        today_str: str
    ) -> Optional[Reward]:
        history = stats.setdefault("history", {})
        yesterday_str = (get_now_beijing() - timedelta(days=1)).strftime("%Y-%m-%d")
        last_checkin = stats.get("last_checkin_date", "")

        reward_obj = None
        if success:
            if last_checkin != today_str:
                if last_checkin == yesterday_str:
                    stats["streak"] = stats.get("streak", 0) + 1
                else:
                    stats["streak"] = 1
                stats["last_checkin_date"] = today_str
                stats["total_success"] = stats.get("total_success", 0) + 1

            r_val, r_unit = extract_reward(result_msg, response_text)
            if r_val is not None:
                old_day = history.get(today_str, {})
                if not old_day.get("reward_counted"):
                    stats["total_reward"] = stats.get("total_reward", 0) + r_val
                if r_unit:
                    stats["reward_unit"] = r_unit
                history[today_str] = {
                    "status": "success",
                    "reward": r_val,
                    "unit": stats.get("reward_unit", r_unit),
                    "reward_counted": True
                }
                reward_obj = Reward(
                    task_id=task.id,
                    task_name=task.name,
                    date=today_str,
                    amount=r_val,
                    unit=r_unit or "点",
                    note=result_msg[:100]
                )
            else:
                if today_str not in history or history[today_str].get("status") != "success":
                    history[today_str] = {"status": "success"}
        else:
            if today_str not in history:
                history[today_str] = {"status": "fail"}

        # 保留最多 60 天战绩记录
        if len(history) > 60:
            sorted_keys = sorted(history.keys())
            for k in sorted_keys[:-60]:
                del history[k]

        return reward_obj

    async def _check_cookie_expiration_warning(self, task: Task, session, now_str: str):
        try:
            params = task.params or {}
            headers = params.get("headers", {})
            has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
            if has_jwt and rem_days is not None and rem_days <= 3.0:
                today_str = now_str.split(" ")[0]
                stats = dict(task.stats or {})
                if stats.get("last_cookie_warn") != today_str:
                    stats["last_cookie_warn"] = today_str
                    task.stats = stats
                    warn_title = f"⚠️【Cookie 即将过期预警】{task.name}"
                    warn_body = (
                        f"任务名称：{task.name}\n"
                        f"预警状态：{'已过期！' if rem_days <= 0 else f'仅剩 {rem_days} 天'}\n"
                        f"到期日期：{exp_date or '未知'}\n"
                        f"登录凭据 (Cookie/Token) 预计将在 **{rem_days} 天** 后失效。\n\n"
                        "💡 建议您抽空在电脑浏览器中重新登录并抓取最新 Cookie 填入任务，以免打卡中断！"
                    )
                    await self.notifier.send(warn_title, warn_body, level="fail")
                    logger.info(f"[Executor] Sent cookie expiration warning for {task.name}, days: {rem_days}")
        except Exception as ex:
            logger.warning(f"[Executor] Cookie warning check error: {ex}")

    def _evaluate_task_response(self, task: Task, status: int, text: str) -> Tuple[bool, str, str]:
        snippet = text[:500]
        params = task.params or {}
        keyword = (params.get("match_keyword") or "").strip()
        ttype = task.type or "checkin"

        extracted_msg = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k in ["message", "msg", "detail", "info", "notice"]:
                    if k in data and isinstance(data[k], str):
                        extracted_msg = data[k]
                        break
        except Exception:
            pass

        detail_suffix = "\n响应内容：" + (extracted_msg if extracted_msg else snippet)
        text_lower = text.lower()

        # 1. Cloudflare 人机挑战
        if "just a moment..." in text_lower or "cf-turnstile" in text_lower or (status == 403 and "cloudflare" in text_lower):
            return False, "❌ 触发网站人机验证拦截(Cloudflare WAF)" + detail_suffix, snippet

        # 2. 凭据失效拦截
        auth_fail_kws = ["未登录", "请先登录", "token expired", "user not found", "未授权", "cookie已失效", "登录超时", "invalid token"]
        for afk in auth_fail_kws:
            if afk in text_lower:
                return False, "❌ 登录凭据失效或未登录 (需更新 Cookie)" + detail_suffix, snippet

        # 3. 打卡幂等成功 ("今天已完成签到")
        if ttype == "checkin":
            already_signed_kws = [
                "今天已完成签到", "已经签到", "已签到", "请勿重复", "明天再来", "明日再来",
                "已经打卡", "今日已打卡", "重复签到", "重复操作", "already signed", "already checked"
            ]
            for ask in already_signed_kws:
                if ask in text:
                    return True, f"今日已完成签到 (包含 '{ask}', HTTP {status}){detail_suffix}", snippet

        # 4. 用户指定关键字匹配
        if keyword:
            keywords = [k.strip() for k in keyword.replace(",", "|").split("|") if k.strip()]
            for kw in keywords:
                if kw in text:
                    return True, f"匹配成功 (包含关键字 '{kw}', HTTP {status}){detail_suffix}", snippet
            return False, f"关键字未匹配 (缺少 '{keyword}', HTTP {status}){detail_suffix}", snippet

        # 5. 常规 HTTP 状态码
        if 200 <= status < 300:
            return True, f"请求成功 (HTTP {status}){detail_suffix}", snippet
        return False, f"HTTP 状态码异常 ({status}){detail_suffix}", snippet

    async def _run_http_task(self, task: Task) -> Tuple[bool, str, str]:
        params = task.params or {}
        url = (params.get("url") or "").strip()
        if not url:
            return False, "URL 为空", "No URL provided"

        method = (params.get("method") or "GET").upper()
        headers = params.get("headers") or {}
        body = params.get("body") or ""
        auth_mode = task.auth_mode or "cookie"

        session_path = None
        if auth_mode == "playwright_account":
            from app.crawler import get_session_path, has_valid_session
            session_path = get_session_path(task.id)
            if not has_valid_session(task.id):
                logger.info(f"[Executor] No valid StorageState found for {task.name}. Triggering auto-login...")
                login_ok, login_msg = await crawler.perform_auto_login(task.id, task.account_config or {}, self.llm_client)
                if not login_ok:
                    return False, f"自动登录失败: {login_msg}", login_msg

        # 调度爬虫执行
        res = await crawler.fetch(
            url=url,
            method=method,
            headers=headers,
            body=body,
            timeout=30,
            force_headless=task.use_headless_browser or (auth_mode == "playwright_account"),
            session_path=session_path
        )

        ok, msg, err = self._evaluate_task_response(task, res.status_code, res.text)

        # 账密模式失效自愈重登
        if not ok and auth_mode == "playwright_account":
            is_auth_error = any(kw in res.text.lower() for kw in ["未登录", "请先登录", "token expired", "cookie已失效", "登录超时", "invalid token"]) or res.status_code in [401, 403]
            if is_auth_error:
                logger.info(f"[Executor] Session expired for {task.name}. Triggering self-healing re-login...")
                login_ok, login_msg = await crawler.perform_auto_login(task.id, task.account_config or {}, self.llm_client)
                if login_ok:
                    res = await crawler.fetch(
                        url=url,
                        method=method,
                        headers=headers,
                        body=body,
                        timeout=30,
                        force_headless=task.use_headless_browser or (auth_mode == "playwright_account"),
                        session_path=session_path
                    )
                    ok, msg, err = self._evaluate_task_response(task, res.status_code, res.text)
                    if ok:
                        msg += " (🔑 账密会话已自动重登自愈刷新)"

        if ok and res.set_cookies:
            if self._merge_set_cookies(task, res.set_cookies):
                msg += " (🍪 凭据已自动续期)"
        return ok, msg, err

    def _merge_set_cookies(self, task: Task, set_cookies: list) -> bool:
        try:
            params = dict(task.params or {})
            headers = dict(params.get("headers") or {})
            current_cookie_str = headers.get("Cookie") or headers.get("cookie") or ""

            cookie_dict = {}
            if current_cookie_str:
                for part in current_cookie_str.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookie_dict[k.strip()] = v.strip()

            updated = False
            for sc in set_cookies:
                first_seg = sc.split(";")[0].strip()
                if "=" in first_seg:
                    k, v = first_seg.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and k.lower() not in ("path", "domain", "expires", "max-age", "samesite", "priority"):
                        if cookie_dict.get(k) != v:
                            cookie_dict[k] = v
                            updated = True

            if updated:
                new_cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                if "cookie" in headers and "Cookie" not in headers:
                    headers["cookie"] = new_cookie_str
                else:
                    headers["Cookie"] = new_cookie_str
                params["headers"] = headers
                task.params = params
                return True
            return False
        except Exception as e:
            logger.warning(f"[Executor] Error merging Set-Cookie: {e}")
            return False

    async def _run_ai_digest_task(self, task: Task) -> Tuple[bool, str, str]:
        params = task.params or {}
        source_url = (params.get("source_url") or "").strip()
        prompt = (params.get("prompt") or "请将以下内容精炼概括为3~5条核心摘要，语言简练客观：").strip()

        # 读取最大截取字节（飞牛物理内存充沛，解除 256KB 限制，设为 0 时表示不截断，安全上限 100MB）
        llm_cfg = await self.llm_client.get_config()
        cfg_max_kb = int(llm_cfg.get("digest_max_kb", 2048))
        if cfg_max_kb <= 0:
            max_bytes = 100 * 1024 * 1024  # 0 为无限制（100MB 极端防护上限）
        else:
            max_bytes = max(10, min(102400, cfg_max_kb)) * 1024


        raw_text = ""
        fetch_mode = "httpx"
        if source_url:
            headers = params.get("headers") or {}
            auth_mode = task.auth_mode or "cookie"
            session_path = None
            if auth_mode == "playwright_account":
                from app.crawler import get_session_path, has_valid_session
                session_path = get_session_path(task.id)
                if not has_valid_session(task.id):
                    logger.info(f"[Executor] AI Digest auto-logging in for {task.name}...")
                    await crawler.perform_auto_login(task.id, task.account_config or {}, self.llm_client)

            logger.info(f"[Executor] AI Digest fetching: {source_url} (force_headless={task.use_headless_browser})")
            res = await crawler.fetch(
                url=source_url,
                method="GET",
                headers=headers,
                timeout=35,
                max_bytes=max_bytes,
                force_headless=task.use_headless_browser or (auth_mode == "playwright_account"),
                session_path=session_path
            )
            raw_text = res.text
            fetch_mode = res.mode_used

        if not raw_text.strip():
            raw_text = params.get("raw_content") or ""

        if not raw_text.strip():
            return False, "未能获取到有效的正文内容进行提炼 (页面内容为空或被拦截)", "Empty content fetched"

        messages = [
            {"role": "system", "content": "你是一个严谨的信息提炼与科技速报分析师。具有强大的学术期刊专刊检索、动态页面解析与要点提炼能力。"},
            {"role": "user", "content": f"{prompt}\n\n【抓取内容 (来源模式: {fetch_mode})】\n{raw_text}"}
        ]

        ok, ai_res = await self.llm_client.chat_completion(messages, timeout=60)
        if ok:
            mode_badge = "🌐 无头浏览器深度渲染" if fetch_mode == "playwright" else "⚡ 极速网络抓取"
            summary_header = f"【信息提炼速报】({mode_badge})\n\n{ai_res}"
            return True, summary_header, ""
        else:
            return False, f"大模型提炼失败: {ai_res}", ai_res

    async def _run_uptime_task(self, task: Task) -> Tuple[bool, str, str]:
        params = task.params or {}
        url = (params.get("target_url") or "").strip()
        if not url:
            return False, "监控目标URL为空", "No target URL"

        res = await crawler.fetch(url, method="GET", timeout=15)
        if 200 <= res.status_code < 400:
            return True, f"服务运行正常 (HTTP {res.status_code}, 延迟 {res.latency_ms}ms)", ""
        else:
            return False, f"服务异常 (HTTP {res.status_code}, 耗时 {res.latency_ms}ms)", f"HTTP Status: {res.status_code}"
