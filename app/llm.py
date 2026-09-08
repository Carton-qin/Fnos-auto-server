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
            "Content-Type": "application/json"
        }
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
                    return False, f"HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            logger.error(f"[LLM] Request failed: {e}")
            return False, f"大模型请求异常: {str(e)}"

    async def diagnose_failure(self, task_name: str, error_details: str) -> str:
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
        headers = {"Authorization": f"Bearer {api_key}"}
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
                return False, [], f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, [], str(e)
