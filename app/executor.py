import json
import time
import asyncio
import logging
import re
from typing import Tuple, Dict, Any, Optional, List
from datetime import datetime, timedelta
from sqlalchemy import select
from app.config import format_now, get_now_beijing
from app.crawler import crawler, extract_reward, get_cookie_expiration_days, clean_html_noise
from app.models import Task, TaskLog, Reward

logger = logging.getLogger("fnos.executor")

def extract_prompt_keywords(prompt: str) -> List[str]:
    """从用户提示词中提取专刊领域核心检索关键词，自动剔除指令词与标点"""
    cleaned_prompt = prompt
    for noise in ["提取并筛选", "提取筛选", "请提取", "请筛选", "请总结", "总结并输出", "相关专刊", "学术专刊", "最新专刊", "专刊征稿"]:
        cleaned_prompt = cleaned_prompt.replace(noise, " ")

    raw_tokens = re.split(r"[/,、\s+;；|]+", cleaned_prompt)
    stop_words = {"提取", "并", "筛选", "专刊", "总结", "分析", "请", "输出", "列出", "关注", "相关", "最新", "信息", "所有", "关于", "订阅", "通知", "检索"}
    clean_kws = []
    for t in raw_tokens:
        t = t.strip()
        if not t or t in stop_words:
            continue
        t = re.sub(r"^(提取|筛选|查找|搜索|关注|订阅)", "", t)
        t = re.sub(r"(专刊|期刊|特刊|征稿)$", "", t)
        if t and t not in stop_words and t not in clean_kws:
            clean_kws.append(t)
    return clean_kws

MAPPED_ACADEMIC_PATTERNS: Dict[str, List[str]] = {
    "锂": [r"\b(lithium|li-ion|li-metal|li-s|li-air)\b", r"锂"],
    "钠": [r"\b(sodium|na-ion|na-s|na-air)\b", r"钠"],
    "碳中和": [r"\b(carbon neutral|net-zero|decarboni[sz]ation)\b", r"碳中和"],
    "低碳": [r"\b(low-carbon|low carbon|green carbon)\b", r"低碳"],
    "碳": [r"\b(carbon|co2)\b", r"碳"],
    "电池": [r"\b(battery|batteries|cell|cells)\b", r"电池"],
    "储能": [r"\b(energy storage|thermal storage)\b", r"储能"],
    "氢": [r"\b(hydrogen|h2)\b", r"氢"],
    "氨": [r"\b(ammonia|nh3)\b", r"氨"],
    "催化": [r"\b(cataly[a-z]*)\b", r"催化"],
    "电催化": [r"\b(electrocataly[a-z]*)\b", r"电催化"],
    "光催化": [r"\b(photocataly[a-z]*)\b", r"光催化"],
    "分离": [r"\b(separation|purification|membrane)\b", r"分离"],
    "膜": [r"\b(membrane[s]?)\b", r"膜"],
    "生物": [r"\b(biomass|biorefinery|bio-based)\b", r"生物"],
    "吸附": [r"\b(adsorption|sorbent)\b", r"吸附"],
    "光伏": [r"\b(photovoltaic|solar|pv)\b", r"光伏"],
    "钙钛矿": [r"\b(perovskite)\b", r"钙钛矿"],
    "材料": [r"\b(material[s]?)\b", r"材料"],
    "转化": [r"\b(conversion)\b", r"转化"],
    "环境": [r"\b(environment[a-z]*)\b", r"环境"],
    "水处理": [r"\b(wastewater|water treatment)\b", r"水处理"],
}

def parse_academic_special_issues(raw_text: str, prompt: str = "", source_url: str = "") -> Tuple[bool, str]:
    """针对学术期刊 Calls for Papers 征稿专刊的本地规则解析器（极速提取、零Token、免疫大模型内容审查拦截）"""
    if not raw_text or ("submission deadline:" not in raw_text.lower() and "calls for papers" not in raw_text.lower()):
        return False, ""

    blocks = re.split(r"(?=Submission deadline:)", raw_text, flags=re.I)
    items: List[Dict[str, Any]] = []

    for b in blocks:
        b_clean = b.strip()
        if not b_clean or "submission deadline:" not in b_clean.lower():
            continue

        lines = [l.strip() for l in b_clean.splitlines() if l.strip()]
        if not lines:
            continue

        m_dl = re.search(r"Submission deadline:\s*([^\n\r]+)", lines[0], re.I)
        deadline = m_dl.group(1).strip() if m_dl else lines[0].replace("Submission deadline:", "").strip()

        title = lines[1] if len(lines) > 1 else "Special Issue"
        title = re.sub(r"^Special issue on\s*", "", title, flags=re.I).strip()

        editors = ""
        journal = ""
        for l in lines[2:]:
            if "guest editors:" in l.lower():
                editors = re.sub(r"^guest editors:\s*", "", l, flags=re.I).strip()
            elif any(kw in l for kw in ["•", "Impact Factor", "CiteScore"]):
                journal = l.strip()
                break

        if not journal and len(lines) > 2:
            journal = lines[2].strip()

        items.append({
            "title": title,
            "deadline": deadline,
            "journal": journal,
            "editors": editors,
        })

    if not items:
        return False, ""

    user_kws = extract_prompt_keywords(prompt)

    # 编译匹配模式
    kw_regex_map = {}
    for uk in user_kws:
        patterns = []
        for m_key, p_list in MAPPED_ACADEMIC_PATTERNS.items():
            if m_key in uk or uk in m_key:
                patterns.extend(p_list)
        if not patterns:
            patterns.append(r"\b" + re.escape(uk) + r"\b")
            patterns.append(re.escape(uk))
        kw_regex_map[uk] = re.compile("|".join(patterns), re.I)

    matched_items = []
    for it in items:
        match_str = f"{it['title']} {it['journal']} {it['editors']}"
        hits = []
        for uk, rgx in kw_regex_map.items():
            if rgx.search(match_str):
                hits.append(uk)
        if hits:
            it["matched_kws"] = hits
            matched_items.append(it)

    target_list = matched_items if matched_items else items[:15]
    total_count = len(items)
    match_count = len(matched_items)

    md = []
    md.append("## 📚 学术专刊征稿精选速报 (🎯 本地高精度规则结构化提取)")
    if source_url:
        md.append(f"> 🔗 **专刊源地址**: [{source_url}]({source_url})")
    if user_kws:
        md.append(f"> 🎯 **订阅检索目标**: `{', '.join(user_kws)}`")
    if matched_items:
        md.append(f"> 📊 **检索统计**: 共扫描到 **{total_count}** 个征稿专刊，精准匹配到 **{match_count}** 个高契合专刊。\n")
    else:
        md.append(f"> 📊 **检索统计**: 共扫描到 **{total_count}** 个征稿专刊（未发现包含目标关键词的专刊，为您精选最新 {len(target_list)} 项）：\n")

    for idx, it in enumerate(target_list, 1):
        md.append(f"### {idx}. {it['title']}")
        md.append(f"- 🏛️ **期刊来源**: {it['journal']}")
        md.append(f"- ⏳ **投稿截止**: **{it['deadline']}**")
        if it['editors']:
            md.append(f"- 👨‍🔬 **客座编辑**: {it['editors']}")
        if it.get("matched_kws"):
            md.append(f"- 🏷️ **命中标签**: `{' / '.join(it['matched_kws'])}`")
        md.append("")

    return True, "\n".join(md)

def clean_academic_content(raw_text: str) -> str:
    """针对学术期刊/专刊征稿页面的智能清洗提取器，剥离冗余的学科导航侧边栏与无关噪音"""
    if not raw_text or len(raw_text.strip()) < 100:
        return raw_text

    raw_lower = raw_text.lower()

    # 针对 ScienceDirect / Elsevier / Springer / Wiley 的 calls-for-papers 专刊列表
    if "calls for papers" in raw_lower or "submission deadline:" in raw_lower:
        marker = -1
        for m_str in [
            "All secondary subject areas selected",
            "Refine calls for papers by",
            "Select secondary Subject area",
            "Filter by keyword"
        ]:
            pos = raw_text.find(m_str)
            if pos != -1:
                marker = pos + len(m_str)
                break

        if marker == -1:
            m_dl = re.search(r"Submission deadline:\s*\d+", raw_text, re.I)
            if m_dl:
                marker = max(0, m_dl.start() - 150)

        content = raw_text[marker:] if marker != -1 else raw_text

        items = []
        blocks = re.split(r"(?=Submission deadline:)", content, flags=re.I)
        for b in blocks:
            b_s = b.strip()
            if "submission deadline:" in b_s.lower():
                lines = [l.strip() for l in b_s.splitlines() if l.strip()]
                valid_lines = [
                    l for l in lines[:7]
                    if not any(ui in l.lower() for ui in ["view all", "show more", "refine calls", "filter by keyword"])
                ]
                if len(valid_lines) >= 2:
                    items.append("\n".join(valid_lines))

        if items:
            return f"【学术专刊征稿列表 (共收录 {len(items)} 个专刊，已剔除无关导航噪音)】\n\n" + "\n\n---\n\n".join(items)

    return raw_text

def filter_by_prompt_keywords(text: str, prompt: str) -> str:
    """根据用户提示词中的学术关键词对专刊条目做精准初筛，极大降低 Token 并规避模型风控误报"""
    kws = extract_prompt_keywords(prompt)

    mapped_kws = list(kws)
    for k in kws:
        if k in MAPPED_ACADEMIC_PATTERNS:
            mapped_kws.append(MAPPED_ACADEMIC_PATTERNS[k][0])

    if "---" in text:
        blocks = text.split("---")
        matched = []
        for b in blocks:
            b_low = b.lower()
            if any(k.lower() in b_low for k in mapped_kws):
                matched.append(b.strip())
        if matched:
            return f"【已根据任务需求关键词 [{', '.join(kws[:6])}] 精准初筛专刊 (共 {len(matched)} 项)】\n\n" + "\n\n---\n\n".join(matched)
    return text

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

            is_academic = any(d in source_url.lower() for d in [
                "sciencedirect.com", "elsevier.com", "springer.com", "wiley.com", "nature.com", "ieee.org"
            ])
            use_headless = task.use_headless_browser or (auth_mode == "playwright_account") or is_academic
            logger.info(f"[Executor] AI Digest fetching: {source_url} (use_headless={use_headless}, is_academic={is_academic})")
            res = await crawler.fetch(
                url=source_url,
                method="GET",
                headers=headers,
                timeout=45,
                max_bytes=max_bytes,
                force_headless=use_headless,
                session_path=session_path
            )
            raw_text = res.text
            fetch_mode = res.mode_used

        if not raw_text.strip():
            raw_text = params.get("raw_content") or ""

        if not raw_text.strip():
            return False, "未能获取到有效的正文内容进行提炼 (页面内容为空或目标服务不可达)", "Empty content fetched"

        # 核心防误报逻辑：校验抓取到的正文是否为 Cloudflare / WAF 拦截页面
        raw_lower = raw_text.lower()
        is_cf_blocked = (
            not res.ok or
            "cloudflare error" in raw_lower or
            "error 100" in raw_lower or
            "error 101" in raw_lower or
            "error 102" in raw_lower or
            "just a moment..." in raw_lower or
            "challenge-running" in raw_lower or
            "cf-turnstile" in raw_lower or
            ("ray id:" in raw_lower and len(raw_text) < 4000)
        )
        if is_cf_blocked:
            error_msg = f"抓取失败：目标站点存在 Cloudflare/WAF 反爬风控拦截 (当前抓取模式: {fetch_mode})。建议确认已开启「无头浏览器深度渲染」并在任务中配置可用 Cookie 或教育网会话凭据。"
            logger.warning(f"[Executor] Task {task.name} ({task.id}) caught Cloudflare block page: {raw_text[:200]}")
            return False, error_msg, raw_text[:500]

        mode_badge = "🌐 无头浏览器深度渲染" if fetch_mode == "playwright" else "⚡ 极速网络抓取"

        # 1. 本地高精度结构化规则解析引擎（针对 ScienceDirect/Elsevier 等学术期刊专刊征稿）
        # 极速、确定性、零Token消耗、且对任何大模型风控审查天然免疫
        has_structured, structured_report = parse_academic_special_issues(raw_text, prompt, source_url)

        if has_structured and structured_report:
            logger.info(f"[Executor] Native structured extractor successfully parsed academic issues for {task.name}.")
            # 准备精炼提取后的专刊摘要尝试由大模型做宏观趋势与热点归纳（限制长度，防整页抓取误触发词汇过滤）
            condensed_digest = structured_report[:3500]
            messages = [
                {"role": "system", "content": "你是一个严谨的信息提炼与科技速报分析师。请对下方精选学术专刊清单进行3~4句核心研究趋势与截稿热点的概括总结，语言简练客观。"},
                {"role": "user", "content": f"{prompt}\n\n【精选学术专刊清单】\n{condensed_digest}"}
            ]
            try:
                ok_llm, ai_insight = await self.llm_client.chat_completion(messages, timeout=25)
            except Exception as e:
                ok_llm, ai_insight = False, str(e)

            # 关键保障：无论大模型是否被服务商审查拦截(content-blocked)或网络超时，本地高精度提取结果均直接作为最终速报输出！
            # 标记为 SUCCESS，正常触发通知推送，彻底消除因模型风控或网络故障导致的任务失败！
            if ok_llm and ai_insight and "content-blocked" not in ai_insight.lower() and not any(kw in ai_insight for kw in ["❌ 抓取失败", "拦截页面"]):
                final_report = f"【信息提炼速报】({mode_badge} · 🎯 本地高精度专刊检索 + 🤖 学术AI研判)\n\n### 💡 核心趋势与截稿研判\n{ai_insight}\n\n---\n\n{structured_report}"
                return True, final_report, ""
            else:
                logger.info(f"[Executor] LLM moderation or error encountered ({ai_insight[:60]}), seamlessly adopting native high-precision report (SUCCESS).")
                final_report = f"【信息提炼速报】({mode_badge} · 🎯 本地高精度结构化智能解析)\n\n{structured_report}"
                return True, final_report, ""

        # 2. 普通非结构化网页或自定义文本的常规大模型提炼流程
        cleaned_text = clean_academic_content(raw_text)

        messages = [
            {"role": "system", "content": "你是一个严谨的信息提炼与科技速报分析师。具有强大的学术期刊专刊检索、动态页面解析与要点提炼能力。"},
            {"role": "user", "content": f"{prompt}\n\n【抓取内容 (来源模式: {fetch_mode})】\n{cleaned_text}"}
        ]

        ok, ai_res = await self.llm_client.chat_completion(messages, timeout=60)

        # 3. 智能容灾恢复：若大模型厂商触发了 content-blocked 安全风控拦截
        if not ok and ("content-blocked" in ai_res.lower() or "data_inspection" in ai_res.lower()):
            logger.warning(f"[Executor] LLM safety guardrail triggered content-blocked for task {task.name}. Activating keyword-focused recovery...")
            focused_text = filter_by_prompt_keywords(cleaned_text, prompt)
            if focused_text and len(focused_text) < len(cleaned_text):
                recovery_messages = [
                    {"role": "system", "content": "你是一个严谨的信息提炼与科技速报分析师。请针对下方已初筛聚焦的学术专刊征稿条目进行提炼总结，按截止日期和期刊进行归纳。"},
                    {"role": "user", "content": f"{prompt}\n\n【初筛聚焦专刊列表】\n{focused_text}"}
                ]
                ok_retry, retry_res = await self.llm_client.chat_completion(recovery_messages, timeout=60)
                if ok_retry:
                    ok = True
                    ai_res = f"*(已自动启用专刊精准净化过滤)*\n\n{retry_res}"
                    logger.info(f"[Executor] Keyword-focused recovery succeeded for task {task.name}!")
                else:
                    ai_res = f"模型内容安全审查拦截 (content-blocked): 建议切换为 DeepSeek 官方或 OpenAI 等对学术词汇无风控拦截的厂商接口。"

        if ok:
            summary_header = f"【信息提炼速报】({mode_badge})\n\n{ai_res}"

            # 语义识别：若大模型指出抓取内容为拦截或失败，不将任务伪标记为成功
            is_ai_failure = any(kw in ai_res for kw in ["❌ 抓取失败", "Cloudflare 拦截页面", "未获取到任何", "访问被拦截", "反爬机制拦截"])
            if is_ai_failure:
                return False, summary_header, "大模型分析确认抓取内容被目标反爬拦截"

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
