"""图像响应解析模块：从 OpenAI / OpenRouter / Gemini 风格响应中提取图片数据。"""

import base64
import re
from typing import Any, Dict, List, Optional

from astrbot import logger

from .image_workflow import ImageWorkflow


async def extract_image_from_markdown(text: str, downloader: Optional[ImageWorkflow]) -> bytes | None:
    """从文本中提取 Markdown 图片并下载。downloader 为 ImageWorkflow 实例（可 None）。"""
    if not text or not downloader:
        return None
    match = re.search(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", text)
    if match:
        url = match.group(1).strip()
        try:
            downloaded = await downloader._download_image(url)
            if downloaded:
                return downloaded
        except Exception as e:
            logger.warning(f"下载 Markdown 图片失败: {e}", exc_info=True)
    return None


async def extract_image_bytes_from_response(data: Dict[str, Any],
                                            downloader: Optional[ImageWorkflow]) -> bytes | str | None:
    """
    从 OpenAI / OpenRouter 风格的响应中提取图像数据。
    兼容多种可能的返回结构，包括：
        - images 直接列表
        - chat.completions 的 choices[].message.images
        - chat.completions 的 choices[].message.content 内嵌
        - data[].url / data[].b64_json
    
    返回值:
        - bytes: 成功下载的图片数据
        - str: 如果是以 "FALLBACK_URL:" 开头的字符串，表示下载失败但有可用URL
        - None: 完全没有找到图像数据
    """
    found_urls: List[str] = []  # 记录所有发现的URL，用于fallback
    
    async def try_download_or_record(url: str) -> bytes | None:
        """尝试下载URL，如果失败则记录URL用于fallback"""
        if url.startswith("data:image/"):
            try:
                return base64.b64decode(url.split(",", 1)[1])
            except Exception:
                return None
        if downloader:
            downloaded = await downloader._download_image(url)
            if downloaded:
                return downloaded
            # 下载失败，记录URL
            found_urls.append(url)
        return None
    
    try:
        # 1. OpenAI Images API 风格 {"data": [{"url": "..."}]} 或 {"data": [{"b64_json": "..."}]}
        if isinstance(data.get("data"), list):
            for item in data["data"]:
                if isinstance(item, dict):
                    if url := item.get("url"):
                        result = await try_download_or_record(url)
                        if result:
                            return result
                    if b64 := item.get("b64_json"):
                        return base64.b64decode(b64)

        # 2. 旧格式 {"images": [{"url": "..."}]}
        if isinstance(data.get("images"), list):
            for image in data["images"]:
                if not isinstance(image, dict):
                    continue
                url = image.get("url")
                if url:
                    result = await try_download_or_record(url)
                    if result:
                        return result

        # 3. Chat Completions 风格
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message", {})

            # 3.1 message.images 显式结构
            if isinstance(message.get("images"), list):
                for image in message["images"]:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("image_url", {}).get("url") or image.get("url")
                    if url:
                        result = await try_download_or_record(url)
                        if result:
                            return result

            content = message.get("content")

            # 3.2 content 为列表（OpenAI 新版多模态结构）
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue

                    item_type = item.get("type")
                    if item_type in {"output_image", "image_url", "image"}:
                        if isinstance(item.get("image_url"), dict):
                            url = item["image_url"].get("url")
                            if url:
                                result = await try_download_or_record(url)
                                if result:
                                    return result
                        if url := item.get("url"):
                            result = await try_download_or_record(url)
                            if result:
                                return result
                        if b64 := item.get("b64_json"):
                            return base64.b64decode(b64)

                    # 兼容部分模型直接返回 base64 文本
                    if item_type in {"text", "output_text"} and isinstance(item.get("text"), str):
                        text_content = item["text"]
                        matches = re.findall(r"data:image/([^;]+);base64,([A-Za-z0-9+/=]+)", text_content)
                        if matches:
                            return base64.b64decode(matches[0][1])
                        markdown_img = await extract_image_from_markdown(text_content, downloader)
                        if markdown_img:
                            return markdown_img

            # 3.3 content 为字符串，尝试匹配其中的 base64 或 Markdown 图片
            if isinstance(content, str):
                matches = re.findall(r"data:image/([^;]+);base64,([A-Za-z0-9+/=]+)", content)
                if matches:
                    return base64.b64decode(matches[0][1])
                markdown_img = await extract_image_from_markdown(content, downloader)
                if markdown_img:
                    return markdown_img
                # 如果Markdown图片下载失败，尝试提取URL作为fallback
                md_match = re.search(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", content)
                if md_match:
                    found_urls.append(md_match.group(1).strip())

        # 如果有发现URL但下载都失败了，返回第一个URL作为fallback
        if found_urls:
            logger.warning(f"图片下载失败，但找到了可用URL: {found_urls[0]}")
            return f"FALLBACK_URL:{found_urls[0]}"
        
        logger.warning(f"未能在响应中提取图像数据，原始响应(截断): {str(data)[:200]}")
        return None
    except Exception as e:
        logger.error(f"解析图像响应时出现错误: {e}", exc_info=True)
        # 即使出错，如果之前记录了URL，也返回它
        if found_urls:
            return f"FALLBACK_URL:{found_urls[0]}"
        return None
