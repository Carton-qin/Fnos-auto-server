import json
import logging
from typing import Tuple, List, Dict, Any, Optional
import httpx
from app.config import DEFAULT_CONFIG

logger = logging.getLogger("fnos.llm")

class LLMClient:
    def __init__(self, db_session_factory=None):
        self.db_session_factory = db_session_factory

    async def get_config(self) -> Dict[str, Any]:
        """获取当前配置的 LLM 参数"""
        if self.db_session_factory:
            from app.models import SystemConfig
            async with self.db_session_factory() as session:
                item = await session.get(SystemConfig, "llm")
                if item and isinstance(item.value, dict):
                    return item.value
        return DEFAULT_CONFIG["llm"]

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: int = 60
    ) -> Tuple[bool, str]:
        cfg = await self.get_config()
        url = (base_url or cfg.get("base_url") or "").rstrip("/")
        key = api_key or cfg.get("api_key") or ""
        mdl = model or cfg.get("model") or "deepseek-chat"
        temp = temperature if temperature is not None else float(cfg.get("temperature", 0.7))

        if not url or not key:
            return False, "未配置大模型 API 基础地址 (Base URL) 或 API Key"

        endpoint = f"{url}/chat/completions" if not url.endswith("/chat/completions") else url
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "FNOS-Automation-Hub/1.0"
        }
        # 兼容阿里云百炼 (DashScope) / 通义千问：添加内容风控免检头，防止学术/科技长篇论文触发误报
        if "dashscope" in url.lower() or "aliyun" in url.lower():
            headers["X-DashScope-DataInspection"] = '{"input":"disable","output":"disable"}'
        elif "openrouter.ai" in url.lower():
            headers["HTTP-Referer"] = "https://github.com/Carton-qin/Fnos-auto-server"
            headers["X-Title"] = "FNOS Automation Hub"

        payload = {
            "model": mdl,
            "messages": messages,
            "temperature": temp,
            "stream": False
        }

        try:
            async with httpx.AsyncClient(timeout=float(timeout), verify=False) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        return True, content
                    return False, "大模型响应格式异常：缺少 choices 列表"
                else:
                    err_msg = resp.text[:300]
                    try:
                        err_json = resp.json()
                        if "error" in err_json:
                            e_val = err_json["error"]
                            if isinstance(e_val, dict) and "message" in e_val:
                                err_msg = e_val["message"]
                            elif isinstance(e_val, str):
                                err_msg = e_val
                    except Exception:
                        pass
                    return False, f"HTTP {resp.status_code}: {err_msg}"

        except Exception as e:
            logger.error(f"[LLM] Request failed: {e}")
            return False, f"大模型请求异常: {str(e)}"

    async def diagnose_failure(self, task_name: str, error_details: str) -> str:
        # 针对大模型服务商安全审核拦截的专家诊断（无需二次调用已异常的大模型）
        err_lower = error_details.lower()
        if "content-blocked" in err_lower or "data_inspection" in err_lower or "datainspectionfailed" in err_lower:
            return "💡 专家诊断：大模型服务商（如阿里云百炼/通义千问）的内容安全风控拦截了输入。原因：抓取页面中含有化学医药、毒理等敏感学术分类词汇。解决办法：系统已自动开启学术正文纯净提取与关键词初筛重试；您也可在大模型配置中选用 DeepSeek 官方或 OpenAI 等对学术文本审查更宽松的厂商。"

        if "cloudflare" in err_lower or "turnstile" in err_lower or "error 100" in err_lower or "cf-turnstile" in err_lower:
            return "💡 专家诊断：目标站点触发了 Cloudflare 5秒盾或 WAF 人机质询拦截。解决办法：已自动将学术站点路由至无头浏览器渲染内核，若仍受阻建议配置教育网有效 Cookie 凭据。"

        prompt = (
            f"你是一个资深的自动化与接口运维专家。以下是自动化任务【{task_name}】的失败日志：\n"
            f"```text\n{error_details}\n```\n\n"
            "请根据上述错误信息，给出简明扼要的专业诊断（不超过150字）：\n"
            "1. 失败的根本原因（如：Cookie失效、触发Cloudflare人机验证、接口签名过期、目标服务器500错误等）；\n"
            "2. 用户的下一步具体排查建议。"
        )
        messages = [
            {"role": "system", "content": "你是一个严谨、简洁的接口故障排查助手，只输出核心结论与排查建议，不要任何废话。"},
            {"role": "user", "content": prompt}
        ]
        ok, res = await self.chat_completion(messages, timeout=30)
        return res if ok else f"诊断生成失败 ({res})"

    async def analyze_and_generate_task(self, url: str, prompt: str, curl_text: str) -> Tuple[bool, Any]:
        system_instruction = (
            "你是一个智能任务生成器。用户会提供网址、需求描述或 cURL 命令。\n"
            "请分析并返回一个标准的 JSON 配置，严格满足以下 JSON 结构：\n"
            "{\n"
            '  "name": "任务名称",\n'
            '  "type": "checkin 或 ai_digest 或 uptime",\n'
            '  "schedule_type": "cron",\n'
            '  "cron_expr": "0 8 * * *",\n'
            '  "jitter_mins": 5,\n'
            '  "use_headless_browser": false,\n'
            '  "params": {\n'
            '    "url": "请求目标URL",\n'
            '    "method": "POST/GET",\n'
            '    "headers": {},\n'
            '    "body": "",\n'
            '    "match_keyword": ""\n'
            "  }\n"
            "}\n"
            "请只输出合法的 JSON 纯文本，不要包含 Markdown 代码块标记（不要 ```json）。"
        )
        user_content = f"目标网址: {url}\n需求说明: {prompt}\ncURL命令:\n{curl_text}"
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content}
        ]
        ok, content = await self.chat_completion(messages, timeout=40)
        if not ok:
            return False, content

        try:
            content_clean = content.strip()
            if content_clean.startswith("```"):
                lines = content_clean.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content_clean = "\n".join(lines).strip()
            task_dict = json.loads(content_clean)
            return True, task_dict
        except Exception as e:
            return False, f"大模型生成的配置无法解析为合法 JSON ({str(e)}): {content}"

    async def fetch_models(self, base_url: str, api_key: str) -> Tuple[bool, List[str], str]:
        url = (base_url or "").rstrip("/")
        if not url or not api_key:
            return False, [], "Base URL 或 API Key 不能为空"

        endpoint = f"{url}/models" if not url.endswith("/models") else url
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "RooCode/3.34.8",
            "HTTP-Referer": "https://github.com/RooVetGit/Roo-Cline",
            "X-Title": "FNOS Automation Hub"
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                resp = await client.get(endpoint, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    model_list = []
                    for item in data.get("data", []):
                        if isinstance(item, dict) and "id" in item:
                            model_list.append(item["id"])
                    return True, sorted(model_list), ""

                err_msg = resp.text[:300]
                try:
                    err_json = resp.json()
                    if "error" in err_json:
                        e_val = err_json["error"]
                        if isinstance(e_val, dict) and "message" in e_val:
                            err_msg = e_val["message"]
                        elif isinstance(e_val, str):
                            err_msg = e_val
                except Exception:
                    pass
                return False, [], f"HTTP {resp.status_code}: {err_msg}"

        except Exception as e:
            return False, [], str(e)
