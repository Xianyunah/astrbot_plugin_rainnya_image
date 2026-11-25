# -*- coding: utf-8 -*-
"""services/media.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

import asyncio
import base64
import copy
import functools
import io
import json
import os
import random
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, File, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from ..constants import *
from ..image_parser import extract_image_bytes_from_response
from ..image_workflow import ImageWorkflow
from ..storage import load_json, save_json


class MediaMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    def _resolve_call_action(self, event: AstrMessageEvent):
        """解析 event 上的 call_action 可调用对象（兼容 bot.call_action / bot.api.call_action）。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        return (getattr(bot, "call_action", None)
                or getattr(getattr(bot, "api", None), "call_action", None)
                or None)


    def _is_auto_recall_enabled(self, event: AstrMessageEvent) -> bool:
        """自动撤回开关：配置开启 且 平台为 aiocqhttp 且 群/会话在白名单内（空=不限制）。

        白名单键格式：f"{platform}:{chat_id}"，chat_id 用群号（群聊）或会话 id（私聊）。
        """
        if not self.conf.get("auto_recall_enabled", False):
            return False
        if event.get_platform_name() != "aiocqhttp":
            return False
        if self._resolve_call_action(event) is None:
            return False
        allowed = self.conf.get("auto_recall_allowed_groups", []) or []
        if not allowed:
            return True
        platform = event.get_platform_name()
        group_id = event.get_group_id()
        chat_id = str(group_id) if group_id else str(getattr(event, "unified_msg_origin", "") or "")
        return f"{platform}:{chat_id}" in [str(x) for x in allowed]


    async def _send_image_get_message_id(self, event: AstrMessageEvent, image_bytes: bytes) -> Optional[str]:
        """aiocqhttp 下直发图片并取 message_id（用于自动撤回）。

        用 call_action send_group_msg / send_private_msg 直发（payload message=[{type:image,...}]），
        拿 data.message_id；失败/非 OneBot 返回 None。
        """
        call = self._resolve_call_action(event)
        if call is None:
            return None
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            message = [{"type": "image", "data": {"file": f"base64://{b64}"}}]
            group_id = event.get_group_id()
            if group_id:
                try:
                    group_id = int(group_id)
                except (TypeError, ValueError):
                    pass
                result = await call("send_group_msg", group_id=group_id, message=message)
            else:
                user_id = event.get_sender_id()
                try:
                    user_id = int(user_id)
                except (TypeError, ValueError):
                    pass
                result = await call("send_private_msg", user_id=user_id, message=message)
            if isinstance(result, dict):
                return result.get("message_id")
            return None
        except Exception as e:
            logger.warning(f"[NewImage] 直发图片取 message_id 失败（降级）: {e}")
            return None


    async def _delete_onebot_message(self, event: AstrMessageEvent, message_id):
        """调用 OneBot delete_msg 撤回消息（失败仅告警）。"""
        if not message_id:
            return
        call = self._resolve_call_action(event)
        if call is None:
            return
        try:
            await call("delete_msg", message_id=message_id)
        except Exception as e:
            logger.warning(f"[NewImage] 自动撤回消息失败: {e}")


    async def _schedule_recall(self, event: AstrMessageEvent, message_id):
        """延迟自动撤回（sleep 后 delete，任务入 _bg_tasks 防 GC）。"""
        if not message_id:
            return
        delay = max(0, int(self.conf.get("auto_recall_delay", 5) or 5))

        async def _recall():
            try:
                await asyncio.sleep(delay)
                await self._delete_onebot_message(event, message_id)
            except Exception as e:
                logger.warning(f"[NewImage] 自动撤回调度异常: {e}")

        task = asyncio.create_task(_recall())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)


    async def _send_image_with_recall(self, event: AstrMessageEvent, image_bytes: bytes,
                                      caption: str) -> Optional[str]:
        """发图 + 自动撤回统一入口。

        返回 None=正常发送（含已调度撤回或已降级）；返回 str=发送失败原因（非空表示失败）。
        自动撤回开启时：OneBot 直发拿 message_id → 调度撤回 → 由调用方 yield 纯文字 caption；
        未开启/非 OneBot：返回特殊标记 "USE_CHAIN_RESULT" 让调用方用原 chain_result 发图+文字。
        """
        if not self._is_auto_recall_enabled(event):
            return "USE_CHAIN_RESULT"
        message_id = await self._send_image_get_message_id(event, image_bytes)
        if message_id is None:
            # 直发失败：降级走原 chain_result（拿不到 id 则不撤回）
            return "USE_CHAIN_RESULT"
        await self._schedule_recall(event, message_id)
        try:
            await event.send(MessageChain(chain=[Plain(caption)]))
        except Exception as e:
            logger.error(f"[NewImage] 自动撤回模式发送 caption 失败: {e}", exc_info=True)
        return None


    async def _send_nsfw_zip(self, event: AstrMessageEvent, image_bytes: bytes,
                             used_model: str, remaining: int) -> None:
        """NSFW 加密模式发送：把生图结果打包成密码加密 ZIP 发送（不直接发图）。

        原始图片已由调用方 _save_generation 落盘时间线，本方法只负责生成并发送加密 ZIP。
        """
        zip_path: Optional[Path] = None
        try:
            # 1. 内存构建密码加密 ZIP（纯 Python ZipCrypto，兼容 Python 3.14+ stdlib 移除写加密的限制）
            zip_data = _build_nsfw_encrypted_zip(image_bytes)
            # 2. 落盘插件数据目录缓存（文件名可读，便于管理；发送后清理）
            cache_dir = Path(self.plugin_data_dir) / NSFW_ZIP_CACHE_DIR
            cache_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = cache_dir / f"nsfw_{ts}.zip"
            zip_path.write_bytes(zip_data)
            # 3. 发送 File 组件（File(name=文件名, file=本地路径)）
            await event.send(MessageChain(chain=[
                File(name=zip_path.name, file=str(zip_path)),
                Plain(f"🔒 NSFW 图片已加密发送，解压密码：{NSFW_ZIP_PASSWORD} | 模型: {used_model} | 剩余: {remaining}次"),
            ]))
            logger.info("[NewImage] NSFW 图片已加密发送（%s）", zip_path.name)
        except Exception as e:
            logger.error(f"[NewImage] NSFW 加密 ZIP 发送失败: {e}", exc_info=True)
            try:
                await event.send(MessageChain(chain=[Plain(f"❌ NSFW 图片加密发送失败: {e}")]))
            except Exception as e2:
                logger.error(f"[NewImage] 发送 NSFW 失败提示失败: {e2}", exc_info=True)
        finally:
            # 4. 发送完成后清理 ZIP 缓存文件（原始图仍在时间线/存档中，管理员可查）
            if zip_path is not None:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.error(f"[NewImage] 清理 NSFW ZIP 缓存失败: {e}")


    async def _extract_event_images(self, event: AstrMessageEvent) -> List[bytes]:
        """从用户当前消息中提取图片字节（引用回复链内的图 + 直接 Image 组件）。

        LLM 工具（generate_image）用：拿到用户附带的参考图做图+文生图；无图返回空列表（纯文生图）。
        注意：不走 self.iwf.get_images 的头像兜底逻辑（LLM 场景不该自动用头像当参考图）。
        无图时尝试 OneBot get_msg 回退（引用消息已过期/仅回复 id 时，从服务器拉取原消息图片）。
        """
        if not self.iwf or not event.message_obj or not getattr(event.message_obj, "message", None):
            return []
        img_bytes_list: List[bytes] = []
        reply_id = None
        try:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and getattr(seg, "chain", None):
                    if getattr(seg, "id", None) and reply_id is None:
                        reply_id = seg.id
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            if img := await self.iwf._image_component_to_bytes(s_chain):
                                img_bytes_list.append(img)
                elif isinstance(seg, Image):
                    if img := await self.iwf._image_component_to_bytes(seg):
                        img_bytes_list.append(img)
        except Exception as e:
            logger.warning(f"[NewImage] 提取当前消息图片失败: {e}", exc_info=True)
        if img_bytes_list:
            return img_bytes_list
        # OneBot get_msg 回退：引用消息只有 id 时拉取原消息图片
        if reply_id is not None:
            img_bytes_list = await self._fetch_reply_images_via_onebot(event, reply_id)
        return img_bytes_list


    async def _fetch_reply_images_via_onebot(self, event: AstrMessageEvent, message_id) -> List[bytes]:
        """通过 OneBot get_msg 拉取指定消息里的图片（打标/图生图引用回退用）。"""
        if not self.iwf:
            return []
        call = self._resolve_call_action(event)
        if call is None:
            return []
        params_list = [{"message_id": message_id}]
        if str(message_id).isdigit():
            params_list.append({"message_id": int(message_id)})
        for params in params_list:
            try:
                result = await call("get_msg", **params)
                if isinstance(result, dict) and "data" in result:
                    result = result["data"]
                message_segments = result.get("message") if isinstance(result, dict) else None
                if not isinstance(message_segments, list):
                    continue
                images: List[bytes] = []
                for item in message_segments:
                    if not isinstance(item, dict) or item.get("type") != "image":
                        continue
                    data = item.get("data") or {}
                    # file 常是缓存文件名（abc.jpg / xxxx.image），Path.is_file() 为 False；
                    # 分别尝试：file 是本机文件则读，否则回退 url（二者互不阻塞）
                    file_val = str(data.get("file") or "").strip()
                    url_val = str(data.get("url") or "").strip()
                    raw = None
                    if file_val:
                        try:
                            file_path = Path(file_val)
                            if file_path.is_file():  # 仅当确为本机文件才读，缓存名/相对路径跳过
                                loop = asyncio.get_running_loop()
                                raw = await loop.run_in_executor(None, file_path.read_bytes)
                        except Exception:
                            raw = None
                    if raw is None and url_val:
                        try:
                            raw = await self.iwf._download_image(url_val)
                        except Exception:
                            raw = None
                    if raw:
                        images.append(raw)
                if images:
                    return images
            except Exception as e:
                logger.warning(f"[NewImage] get_msg 拉取回复图片失败: {e}")
                continue
        return []


    async def _recall_log_message(self, event: AstrMessageEvent, message_id):
        """撤回之前发送的日志消息（仅支持 aiocqhttp 平台；拿不到消息 id 则跳过）"""
        if not message_id:
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        try:
            # 兼容 bot.call_action 与 bot.api.call_action 两种形态
            call = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
            if call:
                await call("delete_msg", message_id=message_id)
        except Exception as e:
            logger.error(f"[NewImage] 撤回日志消息失败: {e}", exc_info=True)


    async def _notify_llm_result(self, event: AstrMessageEvent, text: str) -> None:
        """后台生图完成后，把「结果文本」主动回传当前会话，供主 LLM 下一轮回应/引用。

        通过 context.send_message(unified_msg_origin) 发送纯文字；平台不支持（如 qq_official）
        或发送失败时仅 warning 降级，不抛异常、不影响生图主流程。该消息用户侧同样可见。
        """
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo:
                logger.warning("[NewImage] 无 unified_msg_origin，跳过 LLM 结果回传")
                return
            ok = await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            if not ok:
                logger.warning("[NewImage] LLM 结果回传失败：未匹配到平台 (%s)", umo)
        except Exception as e:
            logger.warning(f"[NewImage] LLM 结果回传异常: {e}")


    def _effective_model_is_gemini(self, group_id: Optional[str] = None,
                                   model_override: Optional[str] = None) -> bool:
        """判断「最终生效模型」是否属于 type=gemini 的提供商。

        模型优先级与 _call_api 一致：model_override > 本群 group_models > available_model_names[0]。
        无法确定模型时回退：存在任一启用且 type=gemini 的提供商即视为可用。
        （is_gemini_api 已废弃为历史配置，不再作为唯一闸门，以 providers.json 的 provider.type 为准。）
        """
        model_name = (model_override or "").strip()
        if not model_name and group_id:
            model_name = self.group_models.get(group_id, "")
        if not model_name and self.available_model_names:
            model_name = self.available_model_names[0]
        if not model_name:
            return any(p.get("type") == "gemini" and p.get("enabled", True)
                       for p in self.providers)
        provider = self._find_provider_for_model(model_name)
        return bool(provider and provider.get("type") == "gemini" and provider.get("enabled", True))


    def _extract_image_size(self, text: str, sender_id: str,
                            group_id: Optional[str] = None,
                            model_override: Optional[str] = None) -> tuple:
        """
        从用户输入中提取分辨率参数（仅白名单用户 + 该模型所属提供商为 Gemini 时生效）。

        返回: (cleaned_text, image_size)
            - cleaned_text: 移除了分辨率关键词后的文本
            - image_size: 提取到的分辨率（如 "2K", "4K"）或 None
        """
        # 闸门：最终生效模型所属提供商 type 必须为 gemini 才启用 2K/4K 白名单提取
        # （openai/novalai 提供商模型不启用；is_gemini_api 历史配置不再参与判断）
        if not self._effective_model_is_gemini(group_id=group_id, model_override=model_override):
            return text, None

        # 检查用户是否在高分辨率白名单中
        if sender_id not in self.hires_whitelist:
            return text, None

        # 匹配 2K/4K（不区分大小写，支持独立出现或在开头/结尾）
        match = re.search(r'\b([24][kK])\b', text)
        if match:
            size_str = match.group(1).upper()
            # 从文本中移除匹配到的分辨率关键词
            cleaned = text[:match.start()] + text[match.end():]
            cleaned = cleaned.strip()
            # 清理多余的空格
            cleaned = re.sub(r'\s{2,}', ' ', cleaned)
            logger.info(f"[NewImage] 白名单用户 {sender_id} 提取分辨率参数: {size_str}")
            return cleaned, size_str

        return text, None

