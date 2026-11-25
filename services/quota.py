# -*- coding: utf-8 -*-
"""services/quota.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class QuotaMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids


    def _is_admin_id(self, uid: str) -> bool:
        """判断某 QQ 号是否为全局管理员（用于查询时显示豁免）。"""
        admin_ids = self.context.get_config().get("admins_id", [])
        return str(uid) in [str(x) for x in admin_ids]


    async def _load_user_counts(self):
        data = await load_json(self.user_counts_file)
        if isinstance(data, dict):
            self.user_counts = {str(k): v for k, v in data.items()}
        else:
            self.user_counts = {}


    async def _save_user_counts(self):
        # 说明：调用方须已持 self._counts_lock（保证 check-modify-write 原子），此处不再重复加锁
        await save_json(self.user_counts_file, self.user_counts)


    def _get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)


    async def _decrease_user_count(self, user_id: str, amount: int = 1):
        # 说明：由持 self._counts_lock 的调用方（_consume_quota）调用，避免嵌套死锁
        user_id_str = str(user_id)
        amount = max(1, int(amount))
        count = self._get_user_count(user_id_str)
        if count > 0:
            self.user_counts[user_id_str] = max(0, count - amount)
            await self._save_user_counts()


    async def _load_group_counts(self):
        data = await load_json(self.group_counts_file)
        if isinstance(data, dict):
            self.group_counts = {str(k): v for k, v in data.items()}
        else:
            self.group_counts = {}


    async def _save_group_counts(self):
        # 说明：调用方须已持 self._counts_lock（保证 check-modify-write 原子），此处不再重复加锁
        await save_json(self.group_counts_file, self.group_counts)


    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)


    async def _decrease_group_count(self, group_id: str, amount: int = 1):
        # 说明：由持 self._counts_lock 的调用方（_consume_quota）调用，避免嵌套死锁
        group_id_str = str(group_id)
        amount = max(1, int(amount))
        count = self._get_group_count(group_id_str)
        if count > 0:
            self.group_counts[group_id_str] = max(0, count - amount)
            await self._save_group_counts()


    def _check_usage_quota(self, sender_id: str, group_id: Optional[str], quota: int = 1) -> str | None:
        """检查次数配额，返回拒绝原因文案；None=可用。管理员豁免由调用方保证。

        quota: 本次生成需消耗的次数（默认 1，多配额模型可传入更大值）。
        语义：只要「个人次数 >= quota」或「群次数 >= quota」任一满足即放行（不叠加扣减）。
        """
        quota = max(1, int(quota))
        user_count = self._get_user_count(sender_id)
        group_count = self._get_group_count(group_id) if group_id else 0
        user_limit_on = self.conf.get("enable_user_limit", True)
        group_limit_on = self.conf.get("enable_group_limit", False) and group_id

        if group_id:
            # 群聊：两种限制都开启时，需要至少一种有次数
            if user_limit_on and group_limit_on:
                if user_count < quota and group_count < quota:
                    return (f"❌ 本群次数与您的个人次数均不足以生成（需 {quota} 次，"
                            f"个人剩 {user_count}，群剩 {group_count}）。\n请使用「喵喵赐福」获取次数。")
            elif user_limit_on and user_count < quota:
                # 只开启用户限制，用户次数不足
                return f"❌ 您的次数不足（需 {quota} 次，当前剩余 {user_count}）。\n请使用「喵喵赐福」获取次数。"
            elif group_limit_on and group_count < quota:
                # 只开启群组限制，群组次数不足
                return f"❌ 本群次数不足（需 {quota} 次，当前剩余 {group_count}）。\n请联系管理员增加群组次数。"
        else:
            # 私聊：只检查用户限制
            if user_limit_on and user_count < quota:
                return f"❌ 您的次数不足（需 {quota} 次，当前剩余 {user_count}）。\n请使用「喵喵赐福」获取次数。"
        return None


    async def _consume_quota(self, sender_id: str, group_id: Optional[str], count: int = 1) -> str:
        """生成成功后扣减次数：群组优先于个人，单一来源扣减（管理员豁免由调用方保证）。

        count: 本次生成实际消耗的次数（默认 1）。
        语义：群次数 >= count 则扣群 count；否则若用户次数 >= count 则扣用户 count（与检查一致，不叠加）。
        用 self._counts_lock 包裹「检查-修改-写盘」，保证并发下原子（decrease_* 内部不加锁避免死锁）。
        返回实际扣减来源：'group' / 'user'；未扣到返回 ''（供调用方展示对应来源的剩余次数）。
        """
        count = max(1, int(count))
        async with self._counts_lock:
            if self.conf.get("enable_group_limit", False) and group_id and self._get_group_count(group_id) >= count:
                await self._decrease_group_count(group_id, amount=count)
                return "group"
            elif self.conf.get("enable_user_limit", True) and self._get_user_count(sender_id) >= count:
                await self._decrease_user_count(sender_id, amount=count)
                return "user"
        return ""


    async def _load_user_checkin_data(self):
        data = await load_json(self.user_checkin_file)
        if isinstance(data, dict):
            self.user_checkin_data = {str(k): v for k, v in data.items()}
        else:
            self.user_checkin_data = {}


    async def _save_user_checkin_data(self):
        await save_json(self.user_checkin_file, self.user_checkin_data)


    async def _acquire_group_slot(self, group_id: Optional[str]) -> bool:
        if not group_id or self.group_task_limit <= 0:
            return True
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current >= self.group_task_limit:
                return False
            self.group_task_counts[group_id] = current + 1
            logger.debug(f"[FigurinePro] 群 {group_id} 任务占用 {self.group_task_counts[group_id]}/{self.group_task_limit}")
            return True


    async def _release_group_slot(self, group_id: Optional[str]):
        if not group_id or self.group_task_limit <= 0:
            return
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current <= 1:
                self.group_task_counts.pop(group_id, None)
            else:
                self.group_task_counts[group_id] = current - 1
            logger.debug(f"[FigurinePro] 群 {group_id} 任务释放，当前 {self.group_task_counts.get(group_id, 0)}")

