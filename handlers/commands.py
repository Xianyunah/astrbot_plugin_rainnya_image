# -*- coding: utf-8 -*-
"""handlers/commands.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class CommandsMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_figurine_request(self, event: AstrMessageEvent):
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command:
            return
        text = event.message_str.strip()
        if not text: return
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        raw_cmd = text.split()[0].strip()
        clean_cmd = re.sub(r'^[#/!！>>\s]+', '', raw_cmd).strip()
        bnn_command = self.conf.get("extra_prefix", "bnn")
        user_prompt = ""
        is_bnn = False
        extracted_size = None  # 用户指定的分辨率参数
        gen_params: Dict = {}  # 用户通过键:值显式指定的生成参数（显式 > 模型默认）
        if raw_cmd == bnn_command or clean_cmd == bnn_command:
            user_prompt = text[len(raw_cmd):].strip()
            # 清除 @ 文本残留
            user_prompt = re.sub(r'@\S+?\(\d+\)', '', user_prompt).strip()
            user_prompt = re.sub(r'@\S+', '', user_prompt).strip()
            is_bnn = True
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
            # 从 bnn 的自定义 prompt 中提取分辨率（仅白名单用户 + Gemini 提供商）
            user_prompt, extracted_size = self._extract_image_size(user_prompt, sender_id, group_id=group_id)
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
            # 提取生成参数（键:值），校验失败明确报错不生成
            params, cleaned = self._parse_and_validate_params(user_prompt, {})
            if params is None:
                yield event.plain_result(f"❌ {cleaned}")
                return
            gen_params = params
            user_prompt = cleaned
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法> size:4K temperature:0.9")
                return
        elif clean_cmd in self.prompt_map:
            cmd = clean_cmd
            preset_prompt = self.prompt_map.get(cmd)
            extra_text = text[len(raw_cmd):].strip()
            # 清除 @ 文本残留（如 "@昵称(123456)"），避免被当成补充 prompt 传给 API
            extra_text = re.sub(r'@\S+?\(\d+\)', '', extra_text).strip()
            extra_text = re.sub(r'@\S+', '', extra_text).strip()
            if extra_text:
                # 从用户补充文本中提取分辨率（仅白名单用户 + Gemini 提供商）
                extra_text, extracted_size = self._extract_image_size(extra_text, sender_id, group_id=group_id)
                # 提取生成参数（键:值），校验失败明确报错不生成
                params, cleaned_extra = self._parse_and_validate_params(extra_text, {})
                if params is None:
                    yield event.plain_result(f"❌ {cleaned_extra}")
                    return
                gen_params = params
                extra_text = cleaned_extra
                if extra_text:
                    user_prompt = f"{preset_prompt}\n\n[User Additional Instruction]: {extra_text}"
                else:
                    user_prompt = preset_prompt
            else:
                user_prompt = preset_prompt
        else:
            return
        is_master = self.is_global_admin(event)
        if not is_master:
            if sender_id in self.conf.get("user_blacklist", []): return
            if group_id and group_id in self.conf.get("group_blacklist", []): return
            if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get("user_whitelist", []): return
            if group_id and self.conf.get("group_whitelist", []) and group_id not in self.conf.get("group_whitelist",
                                                                                                   []): return
            # 次数配额检查（与文生图 / LLM 工具共用 _check_usage_quota）
            # 按预期模型配额预检（多配额模型按真实消耗扣，见 _consume_quota 处）
            _expected_quota = self._get_model_quota(self._expected_model_name(group_id))
            err = self._check_usage_quota(sender_id, group_id, _expected_quota)
            if err:
                yield event.plain_result(err)
                return
        if not self.iwf or not (img_bytes_list := await self.iwf.get_images(event)):
            if not is_bnn:
                yield event.plain_result("请发送或引用一张图片。");
                return
        images_to_process: List[bytes] = []
        initial_messages: List[str] = []
        display_cmd = cmd
        if is_bnn:
            MAX_IMAGES = MAX_IMAGES_PER_REQUEST
            original_count = len(img_bytes_list) if img_bytes_list else 0
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
            else:
                images_to_process = img_bytes_list or []
        else:
            if not img_bytes_list:
                yield event.plain_result("请发送或引用一张图片。")
                return
            images_to_process = [img_bytes_list[0]]

        task_code = self._new_task_code()
        task_info = {
            "task_code": task_code,
            "user_id": sender_id,
            "group_id": group_id,
            "session_id": event.get_session_id() or "direct",
            "command_type": f"预设[{cmd}]" if not is_bnn else "自定义Prompt",
            "prompt": user_prompt,
            "extracted_size": extracted_size,
            "gen_params": gen_params or {},
            "model": self._expected_model_name(group_id),
            "image_bytes_list": images_to_process,
            "created_at": time.time(),
            "is_master": is_master,
        }
        self._pending_generation_tasks[task_code] = task_info
        title_tag = f"预设 [{cmd}]" if not is_bnn else "自定义生图"
        yield event.plain_result(self._render_task_card(task_info, f"{title_tag} 任务已就绪，请核对或调整参数："))


    @filter.command("生图", prefix_optional=True)
    async def on_generation_interactive(self, event: AstrMessageEvent):
        """交互式生图调参、确认与取消中心（必须【引用回复】机器人发出的调参卡片）。"""
        sender_id = str(event.get_sender_id() or "")
        is_master = self.is_global_admin(event)

        task_code, err = await self._extract_task_code_from_event(event)
        if not task_code:
            yield event.plain_result(err)
            return

        self._clean_expired_tasks()
        task_info = self._pending_generation_tasks.get(task_code)
        if not task_info:
            yield event.plain_result(f"❌ 未找到任务 [{task_code}]（任务可能已过期或已完成），请重新发送生图指令发起新任务喵。")
            return

        if task_info["user_id"] != sender_id and not is_master:
            yield event.plain_result("❌ 该任务由其他成员发起，您无法操作该任务喵。")
            return

        raw_cmd = event.message_str.strip()
        action_text = re.sub(r'^[#/!！>>\s]*生图\s*', '', raw_cmd).strip()
        if not action_text:
            yield event.plain_result(self._render_task_card(task_info, "当前任务状态："))
            return

        parts = action_text.split(maxsplit=1)
        sub_cmd = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""

        if sub_cmd in ("确认", "确认生图", "ok", "start", "run", "go"):
            self._pending_generation_tasks.pop(task_code, None)
            async for res in self._execute_confirmed_generation(event, task_info):
                yield res
            return

        if sub_cmd in ("取消", "取消生图", "cancel", "del", "stop"):
            self._pending_generation_tasks.pop(task_code, None)
            yield event.plain_result(f"✅ 已取消生图任务 [{task_code}]。")
            return

        if sub_cmd in ("模型", "model"):
            if not sub_arg:
                lines = "\n".join(f"[{i+1}] {m['name']} (消耗{m['quota']}次)" for i, m in enumerate(self.available_models))
                yield event.plain_result(f"📋 可用模型列表：\n{lines}\n请回复：>>生图 模型 <序号或名称>")
                return
            new_model = ""
            if sub_arg.isdigit():
                idx = int(sub_arg)
                if 1 <= idx <= len(self.available_models):
                    new_model = self.available_models[idx - 1]["name"]
            else:
                match_m = next((m["name"] for m in self.available_models if m["name"].lower() == sub_arg.lower()), None)
                if match_m:
                    new_model = match_m
                else:
                    new_model = sub_arg
            if new_model:
                task_info["model"] = new_model
                task_info["created_at"] = time.time()
                yield event.plain_result(self._render_task_card(task_info, f"✅ 已更换模型为 [{new_model}]！"))
            else:
                yield event.plain_result(f"❌ 找不到模型 {sub_arg}")
            return

        if sub_cmd in ("参数", "param", "params", "set") or any(k in sub_cmd for k in ("size:", "steps:", "scale:", "temp:", "temperature:", "negative_prompt:")):
            param_str = action_text.removeprefix("参数").strip() if action_text.startswith("参数") else action_text
            params, cleaned = self._parse_and_validate_params(param_str, {"model": "str"})
            if params is None:
                yield event.plain_result(f"❌ 参数格式有误: {cleaned}")
                return
            if "model" in params:
                task_info["model"] = params.pop("model")
            task_info["gen_params"].update(params)
            task_info["created_at"] = time.time()
            yield event.plain_result(self._render_task_card(task_info, "✅ 参数已更新！"))
            return

        yield event.plain_result("💡 指令说明（必须【引用回复】）：\n"
                                 "• 调整参数：>>生图 参数 size:portrait_large steps:28\n"
                                 "• 更换模型：>>生图 模型 2\n"
                                 "• 确认出图：>>生图 确认\n"
                                 "• 取消任务：>>生图 取消")


    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        raw_command = event.message_str.strip()
        prompt = re.sub(r'^[#/!！>>\s]*文生图\s*', '', raw_command).strip()
        sender_id = str(event.get_sender_id() or "")
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)
        session_id = event.get_session_id() or "direct"

        # 兼容旧确认格式：如果用户使用 >>文生图 确认生图 且引用了回复，自动转给生图交互器
        if prompt in ("确认生图", "确认") and any(isinstance(s, Reply) for s in event.message_obj.message):
            async for res in self.on_generation_interactive(event):
                yield res
            return

        if not prompt:
            yield event.plain_result("📝 用法：>>文生图 <提示词内容>\n"
                                     "发送后机器人将发送带【任务校验码】的调参卡片；\n"
                                     "请【引用回复】该卡片并发送 `>>生图 参数 xxxxx` 调参，或 `>>生图 确认` 出图。")
            return

        extracted_size = None
        prompt, extracted_size = self._extract_image_size(prompt, sender_id, group_id=group_id)
        gen_params, prompt = self._parse_and_validate_params(prompt, {"model": "str"})
        if gen_params is None:
            yield event.plain_result(f"❌ {prompt}")
            return
        model_override = ""
        if gen_params and "model" in gen_params:
            model_override = str(gen_params.pop("model") or "").strip()
        if not prompt:
            yield event.plain_result("请提供文生图的描述。")
            return

        image_bytes_list = await self._extract_event_images(event)
        task_code = self._new_task_code()
        task_info = {
            "task_code": task_code,
            "user_id": sender_id,
            "group_id": group_id,
            "session_id": session_id,
            "command_type": "图+文生图" if image_bytes_list else "文生图",
            "prompt": prompt,
            "extracted_size": extracted_size,
            "gen_params": gen_params or {},
            "model": model_override or self._expected_model_name(group_id),
            "image_bytes_list": image_bytes_list,
            "created_at": time.time(),
            "is_master": is_master,
        }
        self._pending_generation_tasks[task_code] = task_info
        yield event.plain_result(self._render_task_card(task_info, "生图任务已就绪，请核对或调整参数："))


    @filter.command("预设添加", aliases={"lm添加", "lma"}, prefix_optional=True)
    async def add_lm_prompt(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        raw = event.message_str.strip()
        if ":" not in raw:
            yield event.plain_result('格式错误, 正确示例:\n#预设添加 姿势表:为这幅图创建一个姿势表, 摆出各种姿势')
            return

        key, new_value = map(str.strip, raw.split(":", 1))
        prompt_list = self.conf.get("prompt_list", [])
        found = False
        for idx, item in enumerate(prompt_list):
            if item.strip().startswith(key + ":"):
                prompt_list[idx] = f"{key}:{new_value}"
                found = True
                break
        if not found: prompt_list.append(f"{key}:{new_value}")

        self.conf["prompt_list"] = prompt_list
        await self.conf.save_config_async()
        await self._load_prompt_map()
        yield event.plain_result(f"✅ 已保存生图预设:\n{key}:{new_value}")


    @filter.command("生图帮助", aliases={"lm帮助", "lmh"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        raw_keyword = event.message_str.strip()

        # 兼容直接发送"生图帮助"而没有附加参数的情况
        keyword = raw_keyword
        for prefix_symbol in ("#", "/", "！", "!"):
            if keyword.startswith(prefix_symbol):
                keyword = keyword[len(prefix_symbol):].strip()
        if keyword in {"", "lm帮助", "lmh", "生图帮助"}:
            # 优先使用配置面板自定义的帮助菜单内容（help_menu），留空则用默认
            custom = (self.conf.get("help_menu") or "").strip()
            if custom:
                yield event.plain_result(custom)
                return
            msg = "📸 【NewImage 生图帮助】\n\n"
            msg += "🎨 图生图：发送图片 + 预设指令\n"
            msg += ">>手办化 / >>Q版化 / >>cos化 / >>真人化\n"
            msg += ">>痛车化 / >>痛屋化 / >>玩偶化 / >>贴纸化\n"
            msg += ">>三视图 / >>角色设定 / >>高清修复 / >>鬼图 等 40+ 预设\n"
            msg += "（>>生图帮助 <预设名> 可查看该预设的提示词）\n\n"
            msg += "✏️ 文生图：\n"
            msg += ">>文生图 <画面描述>\n"
            msg += "示例：>>文生图 size:portrait_medium steps:28 negative_prompt:\"bad hands\" 一个女孩...\n\n"
            msg += "📝 生成参数（可选，加在描述前）：\n"
            msg += "size:分辨率（可选 portrait_small/medium/large、landscape_small/medium/large、square、1K/2K/4K）\n"
            msg += "steps:1-150 ｜ temperature:0-2 ｜ max_tokens:64-16384 ｜ negative_prompt:负面词（含空格请用引号包裹）\n"
            msg += "model:模型名（指定本次生图模型，不填用本群默认）\n\n"
            msg += "🎨 自定义图生图：\n"
            msg += "bnn <你的绘画想法>（可一次带多张图）\n\n"
            msg += "🎁 次数与签到：\n"
            msg += "发送「喵喵赐福」—— 每日签到领次数\n"
            msg += ">>生图查询次数 —— 查询剩余次数\n\n"
            msg += "👑 管理员：\n"
            msg += ">>切换模型 <序号> —— 按群切换生图模型\n"
            msg += ">>生图日志 —— 生图时间线记录（查询/删除）\n"
            msg += ">>预设添加 <名称>:<提示词> —— 增改预设\n"
            msg += ">>生图增加用户次数 / >>生图增加群组次数"
            yield event.plain_result(msg)
            return

        prompt = self.prompt_map.get(keyword)
        if not prompt:
            yield event.plain_result("❌ 未找到此预设指令")
            return
        yield event.plain_result(f"📋 预设 [{keyword}] 的内容:\n{prompt}")


    @filter.regex(r"^[#/!！]?喵喵赐福\s*$")
    async def on_checkin(self, event: AstrMessageEvent):
        """每日签到获取生图次数 - 支持直接发送"喵喵赐福"触发"""
        if not self.conf.get("enable_checkin", False):
            yield event.plain_result("📅 本机器人未开启喵喵赐福功能。")
            return
        user_id = event.get_sender_id()
        today_str = datetime.now().strftime("%Y-%m-%d")
        # 用 _counts_lock 包裹「判-读-写-盘」，保证签到并发下 user_checkin 与 user_counts 修改原子，彻底防止并发重入刷次数
        async with self._counts_lock:
            if self.user_checkin_data.get(user_id) == today_str:
                yield event.plain_result(f"您今天已经领取过喵喵赐福。\n剩余次数: {self._get_user_count(user_id)}")
                return
            current_count = self._get_user_count(user_id)
            if self.user_count_cap > 0 and current_count >= self.user_count_cap:
                yield event.plain_result(f"📦 您的生图次数已存满（{current_count}/{self.user_count_cap}），无法继续累积。\n请先使用后再来签到哦~")
                return
            reward = 0
            if str(self.conf.get("enable_random_checkin", False)).lower() == 'true':
                min_reward = max(1, int(self.conf.get("checkin_random_reward_min", 1)))
                max_reward = max(min_reward, int(self.conf.get("checkin_random_reward_max", 5)))
                reward = random.randint(min_reward, max_reward)
            else:
                reward = int(self.conf.get("checkin_fixed_reward", 3))
            new_count = current_count + reward
            # 如果设置了上限，截断到上限
            if self.user_count_cap > 0 and new_count > self.user_count_cap:
                new_count = self.user_count_cap
                actual_reward = new_count - current_count
                self.user_counts[user_id] = new_count
                await self._save_user_counts()
                self.user_checkin_data[user_id] = today_str
                await self._save_user_checkin_data()
                yield event.plain_result(f"🎉 喵喵赐福成功！获得大香蕉生图 {actual_reward} 次（已达上限 {self.user_count_cap}），当前生图剩余: {new_count} 次。")
            else:
                self.user_counts[user_id] = new_count
                await self._save_user_counts()
                self.user_checkin_data[user_id] = today_str
                await self._save_user_checkin_data()
                cap_info = f"（上限 {self.user_count_cap}）" if self.user_count_cap > 0 else ""
                yield event.plain_result(f"🎉 喵喵赐福成功！获得大香蕉生图 {reward} 次，当前生图剩余: {new_count}{cap_info} 次。")


    @filter.command("生图增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        cmd_text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target_qq, count = None, 0
        if at_seg:
            target_qq = str(at_seg.qq)
            match = re.search(r"(\d+)\s*$", cmd_text)
            if match: count = int(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+)", cmd_text)
            if match: target_qq, count = match.group(1), int(match.group(2))
        if not target_qq or count <= 0:
            yield event.plain_result(
                '格式错误:\n#生图增加用户次数 @用户 <次数>\n或 #生图增加用户次数 <QQ号> <次数>')
            return
        async with self._counts_lock:
            current_count = self._get_user_count(target_qq)
            new_count = current_count + count
            # 管理员手动增加不受次数上限（user_count_cap）限制
            self.user_counts[str(target_qq)] = new_count
            await self._save_user_counts()
        yield event.plain_result(f"✅ 已为用户 {target_qq} 增加次数，TA当前剩余 {new_count} 次（管理员增加不受上限限制）。")


    @filter.command("生图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result('格式错误: #生图增加群组次数 <群号> <次数>')
            return
        target_group, count = match.group(1), int(match.group(2))
        async with self._counts_lock:
            current_count = self._get_group_count(target_group)
            self.group_counts[str(target_group)] = current_count + count
            await self._save_group_counts()
        yield event.plain_result(f"✅ 已为群组 {target_group} 增加 {count} 次，该群当前剩余 {current_count + count} 次。")


    @filter.command("生图查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        user_id_to_query = event.get_sender_id()
        if self.is_global_admin(event):
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            if at_seg:
                user_id_to_query = str(at_seg.qq)
            else:
                match = re.search(r"(\d+)", event.message_str)
                if match: user_id_to_query = match.group(1)
        if self._is_admin_id(user_id_to_query):
            personal_display = "∞（管理员豁免）"
        else:
            personal_display = str(self._get_user_count(user_id_to_query))
        reply_msg = f"用户 {user_id_to_query} 个人剩余次数为: {personal_display}"
        if user_id_to_query == event.get_sender_id(): reply_msg = f"您好，您当前个人剩余次数为: {personal_display}"
        if group_id := event.get_group_id(): reply_msg += f"\n本群共享剩余次数为: {self._get_group_count(group_id)}"
        yield event.plain_result(reply_msg)


    @filter.command("生图添加key", prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        new_keys = event.message_str.strip().split()
        if not new_keys: yield event.plain_result("格式错误，请提供要添加的Key。"); return
        api_keys = self.conf.get("api_keys", [])
        added_keys = [key for key in new_keys if key not in api_keys]
        api_keys.extend(added_keys)
        self.conf["api_keys"] = api_keys
        await self.conf.save_config_async()
        yield event.plain_result(f"✅ 操作完成，新增 {len(added_keys)} 个Key，当前共 {len(api_keys)} 个。")


    @filter.command("生图key列表", prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        api_keys = self.conf.get("api_keys", [])
        if not api_keys: yield event.plain_result("📝 暂未配置任何 API Key。"); return
        key_list_str = "\n".join(f"{i + 1}. {key[:8]}...{key[-4:]}" for i, key in enumerate(api_keys))
        yield event.plain_result(f"🔑 API Key 列表:\n{key_list_str}")


    @filter.command("生图删除key", prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        param = re.sub(r'^[#/!！>>\s]*生图删除key\s*', '', event.message_str.strip()).strip()
        api_keys = self.conf.get("api_keys", [])
        if param.lower() == "all":
            self.conf["api_keys"] = []
            await self.conf.save_config_async()
            yield event.plain_result(f"✅ 已删除全部 {len(api_keys)} 个 Key。")
        elif param.isdigit() and 1 <= int(param) <= len(api_keys):
            removed_key = api_keys.pop(int(param) - 1)
            self.conf["api_keys"] = api_keys
            await self.conf.save_config_async()
            yield event.plain_result(f"✅ 已删除 Key: {removed_key[:8]}...")
        else:
            yield event.plain_result("格式错误，请使用 #生图删除key <序号|all>")


    @filter.command("切换模型", prefix_optional=True)
    async def on_switch_model(self, event: AstrMessageEvent):
        """按群切换生图模型（仅管理员）"""
        if not self.is_global_admin(event): return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用本指令")
            return
        if not self.available_models:
            yield event.plain_result("📝 尚未配置可用生图模型，请在插件配置的「model」项中每行填写一个模型。")
            return
        param = re.sub(r'^[#/!！>>\s]*切换模型\s*', '', event.message_str.strip()).strip()
        if not param:
            # 无参数：列出模型列表
            current = self.group_models.get(group_id, "")
            lines = "\n".join(
                f"[{idx}] {m['name']}（消耗{m['quota']}次）" + (f" - {m['desc']}" if m.get("desc") else "") + (" ← 当前" if m["name"] == current else "")
                for idx, m in enumerate(self.available_models, 1)
            )
            yield event.plain_result(f"📋 本群可用生图模型：\n{lines}\n回复序号或输入 `>>切换模型 <序号>` 切换")
            return
        if param.isdigit():
            idx = int(param)
            if 1 <= idx <= len(self.available_models):
                model = self.available_models[idx - 1]["name"]
                self.group_models[group_id] = model
                await self._save_group_models()
                logger.info(f"[NewImage] 群 {group_id} 的模型已切换为: {model}")
                yield event.plain_result(f"✅ 本群生图模型已切换为: {model}")
                return
            yield event.plain_result(f"❌ 序号超出范围（1-{len(self.available_models)}），请重新输入。")
            return
        yield event.plain_result("格式: >>切换模型 <序号>")


    @filter.command("画师串", prefix_optional=True)
    async def on_artist_preset(self, event: AstrMessageEvent):
        """画师串预设：列出 / 按群切换 / 关闭。

        - `画师串` → 列出所有预设 + 当前群激活的预设
        - `画师串 <预设名>` → 当前群切换到该预设（写入 group_artist.json）
        - `画师串 off` → 当前群关闭画师串
        切换/关闭仅管理员可用（同「切换模型」）；私聊无法按群配置。
        """
        group_id = event.get_group_id()
        param = re.sub(r'^[#/!！>>\s]*画师串\s*', '', event.message_str.strip()).strip()
        if not group_id:
            # 列出仍可用（公共信息），切换需群聊
            if not param:
                current = self.group_artists.get("", "")
                lines = "\n".join(
                    f"- {p['name']}" + (f"（负面: {p['negative']}）" if p.get("negative") else "")
                    for p in self.artist_presets
                ) if self.artist_presets else "（暂无画师串预设，请在 WebUI 画师串管理中配置）"
                yield event.plain_result(f"🎨 可用画师串预设：\n{lines}")
                return
            yield event.plain_result("画师串按群切换仅支持在群聊中使用")
            return
        if not param:
            # 列出所有预设 + 当前群激活
            current = self.group_artists.get(str(group_id), "")
            if self.artist_presets:
                lines = "\n".join(
                    f"- {p['name']}" + (" ← 当前" if p["name"] == current else "")
                    + (f"（负面: {p['negative']}）" if p.get("negative") else "")
                    for p in self.artist_presets
                )
            else:
                lines = "（暂无画师串预设，请在 WebUI 画师串管理中配置）"
            cur_tip = f"\n当前群激活: {current}" if current and current.lower() != "off" else "\n当前群未启用画师串"
            yield event.plain_result(f"🎨 可用画师串预设：\n{lines}{cur_tip}\n输入 `>>画师串 <预设名>` 切换，`>>画师串 off` 关闭")
            return
        if not self.is_global_admin(event):
            yield event.plain_result("仅管理员可切换画师串预设")
            return
        if param.lower() == "off":
            self.group_artists.pop(str(group_id), None)
            await self._save_group_artists()
            logger.info(f"[NewImage] 群 {group_id} 的画师串已关闭")
            yield event.plain_result("✅ 本群画师串已关闭")
            return
        target = next((p for p in self.artist_presets if p.get("name") == param), None)
        if not target:
            names = "、".join(p.get("name", "") for p in self.artist_presets) or "（暂无预设）"
            yield event.plain_result(f"❌ 未找到画师串预设 [{param}]。可用预设：{names}")
            return
        self.group_artists[str(group_id)] = target["name"]
        await self._save_group_artists()
        logger.info(f"[NewImage] 群 {group_id} 的画师串已切换为: {target['name']}")
        yield event.plain_result(f"✅ 本群画师串已切换为: {target['name']}")


    @filter.command("发图加密", prefix_optional=True)
    async def on_nsfw_zip_switch(self, event: AstrMessageEvent):
        """按群管理 NSFW 加密开关（仅管理员）。

        `发图加密` → 查询本群状态（跟随全局 or 独立覆盖）
        `发图加密 开` → 本群启用 NSFW 加密（覆盖全局）
        `发图加密 关` → 本群禁用 NSFW 加密（LLM 画 NSFW 直接发原图）
        """
        group_id = event.get_group_id()
        param = re.sub(r'^[#/!！>>\s]*发图加密\s*', '', event.message_str.strip()).strip()
        if not group_id:
            yield event.plain_result("发图加密按群管理，仅支持在群聊中使用")
            return
        if not param:
            # 查询：显示当前群状态（跟随全局 or 独立覆盖）
            global_on = bool(self.conf.get("nsfw_zip_enabled", True))
            if str(group_id) in self.group_nsfw_zip:
                val = bool(self.group_nsfw_zip[str(group_id)])
                yield event.plain_result(
                    f"🔒 本群 NSFW 加密：{'开启' if val else '关闭'}（独立覆盖全局）\n"
                    f"全局开关：{'开启' if global_on else '关闭'}\n"
                    f"管理员可用 `>>发图加密 开|关` 切换，`>>发图加密` 查询")
            else:
                yield event.plain_result(
                    f"🔒 本群 NSFW 加密：{'开启' if global_on else '关闭'}（跟随全局）\n"
                    f"管理员可用 `>>发图加密 开|关` 单独覆盖，`>>发图加密` 查询")
            return
        if not self.is_global_admin(event):
            yield event.plain_result("仅管理员可切换 NSFW 加密开关")
            return
        if param in ("开", "on", "true", "1"):
            self.group_nsfw_zip[str(group_id)] = True
            await self._save_group_nsfw_zip()
            logger.info(f"[NewImage] 群 {group_id} 已开启 NSFW 加密")
            yield event.plain_result("✅ 本群已开启 NSFW 加密（LLM 画 NSFW 将加密 ZIP 发送）")
            return
        if param in ("关", "off", "false", "0"):
            self.group_nsfw_zip[str(group_id)] = False
            await self._save_group_nsfw_zip()
            logger.info(f"[NewImage] 群 {group_id} 已关闭 NSFW 加密")
            yield event.plain_result("❌ 本群已关闭 NSFW 加密（LLM 画 NSFW 将直接发原图）")
            return
        yield event.plain_result("格式: >>发图加密 开|关（按群管理 NSFW 加密）")


    @filter.command("打标", prefix_optional=True)
    async def on_tag_image(self, event: AstrMessageEvent):
        """图片反推打标：引用/附带一张图片，LLM 输出六字段标签 JSON。"""
        img_bytes_list = await self._extract_event_images(event)
        if not img_bytes_list:
            yield event.plain_result("📝 请引用回复或附带一张图片，例如 `>>打标` + 回复图片")
            return
        provider_id = str(self.conf.get("tagger_provider_id", "") or "").strip()
        temperature = float(self.conf.get("tagger_temperature", 0.2) or 0.2)
        max_tokens = int(self.conf.get("tagger_max_tokens", 900) or 900)
        raw = await self._llm_generate_text(event, TAGGER_PROMPT_TEMPLATE,
                                            provider_id=provider_id,
                                            temperature=temperature,
                                            max_tokens=max_tokens,
                                            image_bytes_list=img_bytes_list[:1])
        if not raw:
            yield event.plain_result("❌ 打标失败。请确认当前 LLM provider 支持图像输入，并适当调大 tagger_max_tokens。")
            return
        payload = self._parse_json_object(raw)
        normalized = self._normalize_tag_output(payload)
        if normalized is None:
            yield event.plain_result(f"❌ 打标结果解析失败（非 JSON）。原始输出：\n{raw[:500]}")
            return
        yield event.plain_result(self._format_tag_output(normalized))


    @filter.command("画师推荐", prefix_optional=True)
    async def on_artist_recommend(self, event: AstrMessageEvent):
        """画师推荐：LLM 提取需求标签 → 本地预设库模糊匹配候选 → 组合画师串。"""
        if not self.conf.get("artist_recommend_enabled", False):
            yield event.plain_result("画师推荐功能未开启，请在设置中开启 artist_recommend_enabled")
            return
        raw = re.sub(r'^[#/!！>>\s]*画师推荐\s*', '', event.message_str.strip()).strip()
        if not raw:
            yield event.plain_result("📝 用法：`>>画师推荐 <画风需求>` 如：`>>画师推荐 细腻的赛博朋克厚涂`")
            return
        # 1. 提取需求标签
        extract_prompt = EXTRACT_TAGS_TEMPLATE.replace("<<USER_REQUEST>>", raw)
        extracted = await self._llm_generate_text(event, extract_prompt, temperature=0.2, max_tokens=200)
        if not extracted:
            yield event.plain_result("❌ 标签提取失败，请稍后重试。")
            return
        # 2. 本地预设库模糊匹配
        search_text = extracted.lower()
        max_candidates = max(1, int(self.conf.get("artist_recommend_max_candidates", 10) or 10))
        min_match = max(1, int(self.conf.get("artist_recommend_min_match", 3) or 3))
        matched: List[Dict] = []  # [{name, post_count, source}]：本地预设 + Danbooru 兜底候选
        seen_names: set = set()
        for p in self.artist_presets:
            haystack = f"{p.get('name', '')} {p.get('artists', '')}".lower()
            score = sum(1 for kw in re.findall(r"[a-z0-9_@]+", search_text) if kw in haystack)
            if score >= 1 and p.get("name") not in seen_names:
                seen_names.add(p["name"])
                matched.append({"name": p["name"], "post_count": 0, "source": "preset"})
        matched = matched[:max_candidates]
        # 3. 本地匹配不足 → Danbooru 动态搜索兜底（danbooru_enabled 时）
        if len(matched) < min_match and self.conf.get("danbooru_enabled", False):
            danbooru_limit = max(1, int(self.conf.get("danbooru_limit", DANBOORU_DEFAULT_LIMIT)
                                        or DANBOORU_DEFAULT_LIMIT))
            danbooru_results = await self._danbooru_search_artists(extracted, limit=danbooru_limit)
            if not danbooru_results:
                yield event.plain_result("❌ Danbooru 搜索失败，请检查网络或稍后重试。")
                return
            for d in danbooru_results:
                if d.get("name") not in seen_names:
                    seen_names.add(d["name"])
                    matched.append({"name": d["name"], "post_count": int(d.get("post_count", 0) or 0),
                                    "source": "danbooru"})
        if len(matched) < min_match:
            names = "、".join(p.get("name", "") for p in self.artist_presets) or "（无）"
            yield event.plain_result(f"❌ 候选画师不足（需 ≥{min_match}，仅匹配 {len(matched)} 个）。"
                                     f"请先在 WebUI 画师串管理中添加更多预设（或开启 danbooru_enabled 动态搜索）。\n当前库: {names}")
            return
        # 4. 组合画师串
        pool_text = "\n".join(self._format_artist_candidate(c) for c in matched)
        model_version = str(self.conf.get("extra_prefix", "") or "")
        artist_prompt = ARTIST_FROM_POOL_TEMPLATE \
            .replace("<<USER_REQUEST>>", raw) \
            .replace("<<MODEL_VERSION>>", model_version or "V4.5") \
            .replace("<<CANDIDATE_ARTISTS>>", pool_text)
        artist_str = await self._llm_generate_text(event, artist_prompt, temperature=0.6, max_tokens=400)
        if not artist_str:
            yield event.plain_result("❌ 画师串生成失败，请稍后重试。")
            return
        artist_str = self._strip_code_fence(artist_str).strip()
        if not artist_str:
            yield event.plain_result("❌ 画师串生成为空，请稍后重试。")
            return
        self._last_artist_recommendation = artist_str
        preset_count = sum(1 for c in matched if c.get("source") == "preset")
        danbooru_count = len(matched) - preset_count
        source_desc = f"库内 {preset_count} 个预设" + (f" + Danbooru {danbooru_count} 个" if danbooru_count else "")
        yield event.plain_result(
            f"🎨 候选画师串（来自{source_desc}）：\n{artist_str}\n\n"
            f"💡 输入 `>>画师串保存 <名称>` 保存到预设库，或 `>>画师串 <名称>` 直接切换，`>>画师串 off` 放弃")


    @filter.command("画师串保存", prefix_optional=True)
    async def on_artist_save(self, event: AstrMessageEvent):
        """把最近一次画师推荐结果保存到预设库。格式：画师串保存 <名称>"""
        if not self.is_global_admin(event):
            yield event.plain_result("仅管理员可保存画师串预设")
            return
        param = re.sub(r'^[#/!！>>\s]*画师串保存\s*', '', event.message_str.strip()).strip()
        if not param:
            yield event.plain_result("📝 用法：`>>画师串保存 <名称>`（保存最近一次画师推荐结果）")
            return
        # 优先用最近一次画师推荐结果；无则回退内联格式 名称:画师串
        if not self._last_artist_recommendation:
            yield event.plain_result("暂无最近画师推荐结果。请先使用 `>>画师推荐 <需求>` 生成候选画师串，"
                                     "或使用内联格式 `>>画师串保存 <名称>:artist:a, artist:b`")
            return
        self.artist_presets = [p for p in self.artist_presets if p.get("name") != param]
        self.artist_presets.append({"name": param, "artists": self._last_artist_recommendation, "negative": ""})
        await self._save_artist_presets()
        self._last_artist_recommendation = ""
        yield event.plain_result(f"✅ 画师串预设 [{param}] 已保存（来自最近推荐）")


    @filter.command("生图日志", prefix_optional=True)
    async def on_generation_log(self, event: AstrMessageEvent):
        """查询/删除生图日志（仅管理员）"""
        if not self.is_global_admin(event): return
        usage = ("📋 生图日志用法：\n"
                 "查询 [条数] [页码]\n"
                 "查询 全局 [条数] [页码]\n"
                 "单条查询 <时间戳>\n"
                 "删除 <时间戳>\n"
                 "删除 <群号|全局> <开始时间戳> <结束时间戳>")
        raw = re.sub(r'^[#/!！>>\s]*生图日志\s*', '', event.message_str.strip()).strip()
        if not raw:
            yield event.plain_result(usage)
            return
        parts = raw.split()
        action = parts[0]
        if action == "查询":
            await self._handle_log_query(event, parts[1:])
            return
        if action == "单条查询":
            if len(parts) < 2:
                yield event.plain_result("格式: 生图日志 单条查询 <时间戳>")
                return
            record = next((r for r in self.generation_history if r.get("timestamp") == parts[1]), None)
            if not record:
                yield event.plain_result(f"❌ 未找到时间戳为 {parts[1]} 的生图记录。")
                return
            info = (f"🖼 生图记录详情\n"
                    f"时间戳: {record.get('timestamp')}\n"
                    f"群号: {record.get('group_id') or 'private'}\n"
                    f"请求者: {record.get('user_id')}\n"
                    f"模型: {record.get('model')}\n"
                    f"文件: {record.get('file_path')}\n"
                    f"完整提示词: {record.get('prompt')}")
            chain = [Plain(info)]
            fp = record.get("file_path") or ""
            if fp and Path(fp).is_file():
                chain.append(Image.fromFileSystem(fp))
            try:
                await event.send(MessageChain(chain=chain))
            except Exception as e:
                logger.error(f"[NewImage] 发送单条生图日志失败: {e}", exc_info=True)
            return
        if action == "删除":
            if len(parts) == 2:
                ts = parts[1]
                deleted_records = []
                async with self._history_lock:
                    kept = []
                    for r in self.generation_history:
                        if r.get("timestamp") == ts:
                            deleted_records.append(r)
                        else:
                            kept.append(r)
                    if deleted_records:
                        self.generation_history = kept
                        await self._save_generation_history()
                if deleted_records:
                    self._delete_generation_files(deleted_records)
                    yield event.plain_result(f"✅ 已删除 {len(deleted_records)} 条时间戳为 {ts} 的生图记录与关联文件。")
                else:
                    yield event.plain_result(f"❌ 未找到时间戳为 {ts} 的生图记录。")
                return
            if len(parts) == 4:
                scope_key, start_ts, end_ts = parts[1], parts[2], parts[3]
                deleted_records = []
                async with self._history_lock:
                    remaining = []
                    for r in self.generation_history:
                        r_ts = r.get("timestamp", "")
                        in_range = start_ts <= r_ts <= end_ts  # %Y%m%d_%H%M%S_%f 时间戳按字符串比较即时间序（含微秒基本唯一）
                        in_scope = (scope_key == "全局") or (str(r.get("group_id", "")) == str(scope_key))
                        if not (in_range and in_scope):
                            remaining.append(r)
                        else:
                            deleted_records.append(r)
                    if deleted_records:
                        self.generation_history = remaining
                        await self._save_generation_history()
                if deleted_records:
                    self._delete_generation_files(deleted_records)
                    yield event.plain_result(f"✅ 已删除 {len(deleted_records)} 条生图记录与关联文件（范围: {scope_key}, {start_ts} ~ {end_ts}）。")
                else:
                    yield event.plain_result(f"❌ 该范围内没有生图记录。")
                return
            yield event.plain_result("格式: 生图日志 删除 <时间戳> 或 删除 <群号|全局> <开始时间戳> <结束时间戳>")
            return
        yield event.plain_result(usage)


    async def _handle_log_query(self, event: AstrMessageEvent, args: List[str]) -> None:
        """处理生图日志「查询」子命令：本群/全局、条数(≤20)、每页10条翻页，翻页时先撤回上一条日志消息再发新页。"""
        async def _send(msg: str):
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception as e:
                logger.error(f"[NewImage] 发送生图日志失败: {e}", exc_info=True)

        scope = "group"
        count = 10
        page = 1
        if args and args[0] == "全局":
            scope = "global"
            args = args[1:]
        if args:
            try:
                count = max(1, min(int(args[0]), LOG_QUERY_LIMIT_MAX))
            except ValueError:
                await _send("❌ 条数格式错误（应为数字，最大 20）。")
                return
        if len(args) > 1:
            try:
                page = max(1, int(args[1]))
            except ValueError:
                await _send("❌ 页码格式错误（应为数字）。")
                return
        group_id = event.get_group_id()
        # 时间倒序（最新在前）：generation_history 为 append 顺序，最新在末尾
        records = list(reversed(self.generation_history))
        if scope == "group" and group_id:
            records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
        records = records[:count]
        total_pages = max(1, (len(records) + 9) // 10)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * 10
        page_records = records[start:start + 10]
        if not page_records:
            await _send("📭 暂无生图日志。")
            return
        lines = []
        for r in page_records:
            prompt_short = str(r.get("prompt", ""))[:PROMPT_SHORT_LEN]
            lines.append(f"{r.get('timestamp','?')} | {r.get('file_path','?')} | {prompt_short} | "
                         f"{r.get('group_id','') or 'private'} | {r.get('user_id','?')}")
        msg = (f"📋 生图日志（第 {page}/{total_pages} 页，本页 {len(page_records)} 条，"
               f"范围: {'全局' if scope == 'global' else '本群'}）\n" + "\n".join(lines))
        session_id = event.get_session_id()
        key = (session_id, event.get_sender_id())  # (会话, 发送者) 作键，避免群内跨用户误撤回
        prev = self._log_page_msgid.get(key)
        if page > 1 and prev and prev.get("scope") == scope and prev.get("count") == count:
            # 翻页：先撤回上一条日志消息，再发新页
            # 注：AstrBot 4.27.2 的 event.send 不返回消息 id（返回 None），msgid 恒为 None，
            # _recall_log_message 会因 message_id 为空而自然跳过，实际降级为「直接发新页」；
            # 保留调用与存储结构，供未来平台 API 返回消息 id 时自动启用撤回
            await self._recall_log_message(event, prev.get("msgid"))
        try:
            sent_id = await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception as e:
            logger.error(f"[NewImage] 发送生图日志失败: {e}", exc_info=True)
            return
        # 记录当前日志消息 id 供后续翻页撤回（event.send 返回 None 时即不可撤回）
        self._log_page_msgid[key] = {"msgid": sent_id, "page": page, "scope": scope, "count": count}

