import base64
import logging
from typing import Optional

logger = logging.getLogger("fnos.captcha")

_ocr_instance = None

def get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        try:
            import ddddocr
            _ocr_instance = ddddocr.DdddOcr(show_ad=False)
            logger.info("[Captcha] ddddocr engine initialized successfully")
        except Exception as e:
            logger.warning(f"[Captcha] ddddocr not available: {e}")
            _ocr_instance = False
    return _ocr_instance if _ocr_instance is not False else None

async def solve_captcha_image(image_bytes: bytes, llm_client=None) -> str:
    """
    智能求解图形验证码：
    1. 优先使用本地轻量离线 ddddocr (0延时, 95%+准确率)
    2. 若本地引擎无法识别或置信度异常，尝试调用多模态大模型视觉接口兜底
    """
    if not image_bytes:
        return ""

    ocr = get_ocr()
    if ocr:
        try:
            text = ocr.classification(image_bytes)
            if text and len(text.strip()) > 0:
                logger.info(f"[Captcha] ddddocr successfully solved captcha: {text.strip()}")
                return text.strip()
        except Exception as e:
            logger.warning(f"[Captcha] ddddocr solving failed: {e}")

    # 二级兜底：大模型多模态视觉识别
    if llm_client:
        try:
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请识别并提取此验证码图片中的字符（若是算术题请直接给出计算结果数字），只输出验证码本身，绝对不要任何解释、标点或多余文字："},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}}
                    ]
                }
            ]
            ok, res = await llm_client.chat_completion(messages, timeout=20)
            if ok and res.strip():
                clean_res = res.strip().replace(" ", "").replace("\n", "")
                logger.info(f"[Captcha] Vision LLM solved captcha: {clean_res}")
                return clean_res
        except Exception as ve:
            logger.warning(f"[Captcha] Vision LLM fallback error: {ve}")

    return ""

async def solve_page_captcha(page, selector: str, llm_client=None) -> str:
    """
    在 Playwright 页面中截取指定验证码元素并完成自动求解
    """
    try:
        element = await page.wait_for_selector(selector, timeout=8000)
        if not element:
            logger.warning(f"[Captcha] Captcha element not found by selector: {selector}")
            return ""
        img_bytes = await element.screenshot()
        return await solve_captcha_image(img_bytes, llm_client=llm_client)
    except Exception as e:
        logger.warning(f"[Captcha] Failed to capture or solve captcha on page: {e}")
        return ""
