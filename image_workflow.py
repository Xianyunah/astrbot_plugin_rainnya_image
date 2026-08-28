"""图片获取/下载工作流：从消息中提取图片、下载头像、动图抽帧等。"""

import asyncio
import base64
import io
from pathlib import Path
from typing import List, Optional

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ImageWorkflow:
    def __init__(self, proxy_url: str | None = None, timeout: int = 60, retries: int = 3):
        if proxy_url: logger.info(f"ImageWorkflow 使用代理: {proxy_url}")
        self.session = aiohttp.ClientSession()
        self.proxy = proxy_url
        self.timeout = timeout
        self.retries = retries

    async def _download_image(self, url: str, timeout: Optional[int] = None, max_retries: Optional[int] = None) -> bytes | None:
        """
        下载图片，支持重试机制和更长的超时时间。
        
        Args:
            url: 图片URL
            timeout: 单次请求超时时间（秒），默认使用构造函数传入的值
            max_retries: 最大重试次数，默认使用构造函数传入的值
        """
        timeout = timeout or self.timeout
        max_retries = max_retries if max_retries is not None else self.retries
        
        logger.info(f"正在尝试下载图片: {url}")
        
        last_error = None
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"第 {attempt + 1} 次重试下载: {url}")
                    # 重试前等待一小段时间
                    await asyncio.sleep(1 * attempt)
                
                async with self.session.get(url, proxy=self.proxy, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except aiohttp.ClientResponseError as e:
                last_error = f"HTTP状态码 {e.status}: {e.message}"
                logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
                if e.status in (400, 401, 403, 404):
                    # 这些错误重试也没用
                    break
            except asyncio.TimeoutError:
                last_error = f"请求超时 ({timeout}s)"
                logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
        
        logger.error(f"图片下载最终失败: {last_error}, URL: {url}")
        return None

    async def _get_avatar(self, user_id: str) -> bytes | None:
        if not user_id.isdigit(): logger.warning(f"无法获取非 QQ 平台或无效 QQ 号 {user_id} 的头像。"); return None
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return await self._download_image(avatar_url)

    def _extract_first_frame_sync(self, raw: bytes) -> bytes:
        img_io = io.BytesIO(raw)
        try:
            with PILImage.open(img_io) as img:
                if getattr(img, "is_animated", False):
                    logger.info("检测到动图, 将抽取第一帧进行生成")
                    img.seek(0)
                    first_frame = img.convert("RGBA")
                    out_io = io.BytesIO()
                    first_frame.save(out_io, format="PNG")
                    return out_io.getvalue()
        except Exception as e:
            logger.warning(f"抽取图片帧时发生错误, 将返回原始数据: {e}", exc_info=True)
        return raw

    async def _load_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()
        if Path(src).is_file():
            raw = await loop.run_in_executor(None, Path(src).read_bytes)
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
        if not raw: return None
        return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

    async def _image_component_to_bytes(self, image_comp: Image) -> bytes | None:
        """
        将 Image 组件转换为字节。
        优先使用 AstrBot 提供的 convert_to_base64，兼容 WebChat 等平台的图片来源。
        """
        # 1. 直接尝试 convert_to_base64
        if hasattr(image_comp, "convert_to_base64"):
            try:
                base64_str = await image_comp.convert_to_base64()
                if base64_str:
                    if base64_str.startswith("data:image/"):
                        base64_str = base64_str.split(",", 1)[1]
                    return base64.b64decode(base64_str)
            except Exception as e:
                logger.warning(f"通过 convert_to_base64 获取图片数据失败: {e}", exc_info=True)

        # 2. 回退到原有逻辑
        if image_comp.url:
            return await self._load_bytes(image_comp.url) or None
        if image_comp.file:
            return await self._load_bytes(image_comp.file) or None
        return None

    async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
        img_bytes_list: List[bytes] = []
        at_user_ids: List[str] = []

        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        if img := await self._image_component_to_bytes(s_chain):
                            img_bytes_list.append(img)

        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                if img := await self._image_component_to_bytes(seg):
                    img_bytes_list.append(img)
            elif isinstance(seg, At):
                at_user_ids.append(str(seg.qq))

        if img_bytes_list:
            return img_bytes_list

        if at_user_ids:
            for user_id in at_user_ids:
                if avatar := await self._get_avatar(user_id):
                    img_bytes_list.append(avatar)
            return img_bytes_list

        if avatar := await self._get_avatar(event.get_sender_id()):
            img_bytes_list.append(avatar)

        return img_bytes_list

    async def terminate(self):
        if self.session and not self.session.closed: await self.session.close()
