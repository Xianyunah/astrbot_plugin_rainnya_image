# -*- coding: utf-8 -*-
"""services/interactive.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class InteractiveMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    def _new_task_code(self) -> str:
        """生成 8 位字符数字随机校验码（排除易混淆字符 0/O/1/I）。"""
        chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        self._clean_expired_tasks()
        while True:
            code = "".join(random.choice(chars) for _ in range(8))
            if code not in self._pending_generation_tasks:
                return code


    def _clean_expired_tasks(self) -> None:
        """清理已过期（>600秒）的生图待确认调参任务。"""
        now = time.time()
        expired = [c for c, t in self._pending_generation_tasks.items() if now - float(t.get("created_at", 0)) > 600]
        for c in expired:
            self._pending_generation_tasks.pop(c, None)


    async def _extract_task_code_from_event(self, event: AstrMessageEvent) -> tuple:
        """从引用回复中提取第一行的 8 位生图任务校验码。

        返回 (task_code, error_message)。
        若成功提取返回 (code, None)；若未引用或提取失败返回 (None, err)。
        """
        reply_seg = next((s for s in event.message_obj.message if isinstance(s, Reply)), None)
        if not reply_seg:
            return None, "⚠️ 必须【引用回复】机器人发送的包含【任务校验码】的消息才能调整参数或确认生图喵！"

        reply_text = ""
        if getattr(reply_seg, "chain", None):
            for item in reply_seg.chain:
                if isinstance(item, Plain):
                    reply_text += item.text
                elif isinstance(item, str):
                    reply_text += item

        reply_id = getattr(reply_seg, "id", None)
        if not reply_text and reply_id is not None:
            call = self._resolve_call_action(event)
            if call:
                try:
                    res = await call("get_msg", message_id=reply_id)
                    if isinstance(res, dict) and "data" in res:
                        res = res["data"]
                    if isinstance(res, dict):
                        segs = res.get("message")
                        if isinstance(segs, list):
                            for s in segs:
                                if isinstance(s, dict) and s.get("type") == "text":
                                    reply_text += s.get("data", {}).get("text", "")
                        elif isinstance(res.get("raw_message"), str):
                            reply_text = res["raw_message"]
                except Exception as e:
                    logger.debug(f"[NewImage] get_msg 获取引用文本失败: {e}")

        # 正则匹配第一行的任务校验码
        match = re.search(r"任务校验码[:：\s]*([A-Za-z0-9]{8})", reply_text)
        if match:
            return match.group(1).upper(), None

        raw_cmd = event.message_str.strip()
        code_match = re.search(r"\b([23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8})\b", raw_cmd.upper())
        if code_match and code_match.group(1) in self._pending_generation_tasks:
            return code_match.group(1), None

        return None, "❌ 引用的消息中未找到有效的 8 位【任务校验码】，请确认是否引用了正确的生图调参消息喵。"


    def _render_task_card(self, task_info: Dict[str, Any], title: str = "生图任务已就绪，请核对或调整参数：") -> str:
        """渲染标准化调参卡片，第一行严格呈现【任务校验码: XXXXXXXX】。"""
        code = task_info["task_code"]
        prompt = task_info.get("prompt", "")
        preview = prompt[:DISPLAY_PROMPT_LEN] + "..." if len(prompt) > DISPLAY_PROMPT_LEN else prompt
        model = task_info.get("model", "")
        quota = self._get_model_quota(model)
        gen_params = task_info.get("gen_params", {})
        image_bytes_list = task_info.get("image_bytes_list", [])
        has_ref = bool(image_bytes_list)
        mode = "图+文生图" if has_ref else task_info.get("command_type", "文生图")

        lines = [
            f"【任务校验码: {code}】",
            f"🎨 {title}",
            "━━━━━━━━━━━━━━",
            f"📝 模式：{mode}",
            f"💬 提示词：{preview}",
            f"🤖 当前模型：{model}（消耗 {quota} 次配额）",
        ]
        if has_ref:
            lines.append(f"🖼️ 参考图：{len(image_bytes_list)} 张")

        param_desc = []
        if task_info.get("extracted_size"):
            param_desc.append(f"size={task_info['extracted_size']}")
        for k, v in gen_params.items():
            param_desc.append(f"{k}={v}")
        if param_desc:
            lines.append("⚙️ 当前参数：" + ", ".join(param_desc))
        else:
            lines.append("⚙️ 当前参数：默认设置")

        lines.extend([
            "━━━━━━━━━━━━━━",
            "💡 调参及确认说明（必须【引用回复】本条消息）：",
            "• 调整参数：>>生图 参数 size:portrait_large steps:28",
            "• 更换模型：>>生图 模型 2（或具体模型名）",
            "• 确认出图：>>生图 确认",
            "• 取消任务：>>生图 取消",
            "（⏳ 10分钟内有效，支持多次调整参数）",
        ])
        return "\n".join(lines)


    async def _execute_confirmed_generation(self, event: AstrMessageEvent, task_info: Dict[str, Any]):
        """执行已确认的生图任务（统一配额、并发槽位、生图与落盘发送）。"""
        sender_id = task_info["user_id"]
        group_id = task_info.get("group_id")
        is_master = task_info.get("is_master", False)
        prompt = task_info.get("prompt", "")
        extracted_size = task_info.get("extracted_size")
        gen_params = task_info.get("gen_params", {})
        model_override = task_info.get("model", "")
        image_bytes_list = task_info.get("image_bytes_list", [])
        has_reference_image = bool(image_bytes_list)

        if not is_master:
            if sender_id in self.conf.get("user_blacklist", []): return
            if group_id and group_id in self.conf.get("group_blacklist", []): return
            if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get("user_whitelist", []): return
            if group_id and self.conf.get("group_whitelist", []) and group_id not in self.conf.get("group_whitelist", []): return

            _expected_quota = self._get_model_quota(model_override or self._expected_model_name(group_id))
            err = self._check_usage_quota(sender_id, group_id, _expected_quota)
            if err:
                yield event.plain_result(err)
                return

        display_prompt = prompt[:DISPLAY_PROMPT_LEN] + '...' if len(prompt) > DISPLAY_PROMPT_LEN else prompt
        slot_acquired = False
        try:
            if not await self._acquire_group_slot(group_id):
                if self.group_task_limit > 0:
                    yield event.plain_result(f"⚠️ 当前本群已有 {self.group_task_limit} 个生成任务正在处理，请稍后再试。")
                else:
                    yield event.plain_result("⚠️ 当前生成任务过多，请稍后再试。")
                return
            slot_acquired = True

            mode_label = "图+文生图" if has_reference_image else "文生图"
            image_label = f"（检测到 {len(image_bytes_list)} 张参考图）" if has_reference_image else ""
            yield event.plain_result(f"🎨 收到{mode_label}请求{image_label}，正在生成 [{display_prompt}]...")

            start_time = datetime.now()
            res, used_model = await self._call_api(image_bytes_list, prompt, image_size=extracted_size, group_id=group_id,
                                                   gen_params=gen_params or None, size_strict=True,
                                                   model_override=model_override or None)
            elapsed = (datetime.now() - start_time).total_seconds()

            if isinstance(res, bytes):
                if not is_master:
                    await self._consume_quota(sender_id, group_id, count=self._get_model_quota(used_model))
                try:
                    await self._save_generation(group_id, sender_id, prompt, used_model, res)
                except Exception as e:
                    logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)

                caption_parts = [f"✅ {'图+文生图' if has_reference_image else '文生图'}成功 ({elapsed:.2f}s)", f"模型: {used_model}"]
                if is_master:
                    caption_parts.append("剩余次数: ∞")
                else:
                    if self.conf.get("enable_user_limit", True): caption_parts.append(
                        f"个人剩余: {self._get_user_count(sender_id)}")
                    if self.conf.get("enable_group_limit", False) and group_id: caption_parts.append(
                        f"本群剩余: {self._get_group_count(group_id)}")
                caption = " | ".join(caption_parts)
                send_res = await self._send_image_with_recall(event, res, caption)
                if send_res == "USE_CHAIN_RESULT":
                    yield event.chain_result([Image.fromBytes(res), Plain(caption)])
                elif send_res:
                    yield event.plain_result(f"❌ 发送失败: {send_res}")
            else:
                yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")
        finally:
            if slot_acquired:
                await self._release_group_slot(group_id)
        event.stop_event()

