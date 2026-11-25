# -*- coding: utf-8 -*-
"""handlers/web_api.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class WebAPIMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    def _register_web_apis(self) -> None:
        """注册插件 WebUI 后端 API（仅注册一次，重载插件时幂等）。"""
        if self._web_registered:
            return
        self._web_registered = True
        base = f"/{self.WEB_PLUGIN_NAME}"
        endpoints = [
            (f"{base}/models", self._web_models_get, ["GET"], "获取可用模型列表（含配额/说明，兼容旧前端：返回第一个启用提供商）"),
            (f"{base}/models", self._web_models_post, ["POST"], "保存可用模型列表（已迁移到提供商管理，返回引导错误）"),
            (f"{base}/providers", self._web_providers_get, ["GET"], "获取提供商列表（API Key 掩码）"),
            (f"{base}/providers", self._web_providers_post, ["POST"], "新增提供商"),
            (f"{base}/providers/delete", self._web_providers_delete, ["POST"], "删除提供商（含其模型）"),
            (f"{base}/providers/save", self._web_providers_save, ["POST"], "更新提供商配置（api_key 传空串表示不修改）"),
            (f"{base}/providers/models", self._web_providers_models_get, ["GET"], "获取指定提供商的模型列表"),
            (f"{base}/providers/models", self._web_providers_models_post, ["POST"], "保存指定提供商的模型列表"),
            (f"{base}/users", self._web_users_get, ["GET"], "获取用户次数列表（分页/搜索）"),
            (f"{base}/users/add", self._web_users_add, ["POST"], "增加用户次数（不受上限限制）"),
            (f"{base}/users/set", self._web_users_set, ["POST"], "设置用户次数为指定值（0 或任意非负整数）"),
            (f"{base}/groups", self._web_groups_get, ["GET"], "获取群组次数列表（分页/搜索）"),
            (f"{base}/groups/add", self._web_groups_add, ["POST"], "增加群组次数"),
            (f"{base}/groups/set", self._web_groups_set, ["POST"], "设置群组次数为指定值（0 或任意非负整数）"),
            (f"{base}/logs", self._web_logs_get, ["GET"], "获取生图时间线（范围/天数/分页）"),
            (f"{base}/logs/image", self._web_logs_image, ["GET"], "获取生图图片（路径校验 + base64）"),
            (f"{base}/logs/delete", self._web_logs_delete, ["POST"], "删除生图时间线记录（按时间戳或范围）"),
            (f"{base}/config", self._web_config_get, ["GET"], "获取全部设置（API 配置已迁移到提供商管理）"),
            (f"{base}/config", self._web_config_post, ["POST"], "保存全部设置（白名单字段，一次性原子落盘）"),
            (f"{base}/artist-presets", self._web_artist_presets_get, ["GET"], "获取画师串预设库"),
            (f"{base}/artist-presets", self._web_artist_presets_post, ["POST"], "保存画师串预设库"),
            (f"{base}/artist-presets/export", self._web_artist_presets_export, ["GET"], "导出画师串预设库为 JSON"),
            (f"{base}/artist-presets/import", self._web_artist_presets_import, ["POST"], "智能导入画师串预设（支持Rainnya/AstrBot幻笔/云崽幻笔）"),
            (f"{base}/artist-presets/detect-nai-brush", self._web_artist_presets_detect_nai_brush, ["GET"], "探测本地已安装的 AstrBot 幻笔插件画师预设"),
            (f"{base}/artist-presets/group", self._web_artist_groups_get, ["GET"], "获取各群画师串激活映射"),
            (f"{base}/artist-presets/group", self._web_artist_groups_post, ["POST"], "设置某群画师串预设"),
            (f"{base}/workbench/models", self._web_workbench_models, ["GET"], "获取生图测试台可用模型与提供商详情"),
            (f"{base}/workbench/generate", self._web_workbench_generate, ["POST"], "生图测试台出图接口"),
        ]
        for route, handler, methods, desc in endpoints:
            self.context.register_web_api(route, handler, methods, desc)


    def _web_require_login(self):
        """校验请求来自 Dashboard 登录用户（request.username 存在）。返回 None 表示放行，否则返回错误响应。"""
        try:
            username = request.username
        except Exception:
            username = None
        if not username:
            return error_response("未登录，禁止访问（仅 Dashboard 登录用户可调用）", status_code=403)
        return None


    async def _web_models_get(self):
        """兼容旧前端：返回第一个启用提供商的模型列表（未迁移的旧面板仍可读）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            target = next((p for p in self.providers if p.get("enabled", True)), None)
            if not target:
                return json_response({"models": []})
            models = [
                {"name": m["name"], "quota": int(m.get("quota", 1)), "desc": str(m.get("desc", "") or ""),
                 "params": dict(m.get("params") or {}),
                 "allowed_sizes": m.get("allowed_sizes")}
                for m in (target.get("models") or [])
                if isinstance(m, dict) and m.get("name")
            ]
            return json_response({"models": models})
        except Exception as e:
            return error_response(str(e))


    async def _web_models_post(self):
        """模型管理已迁移到提供商管理：旧 POST /models 直接返回引导错误，避免误写旧 conf.model。"""
        if (resp := self._web_require_login()):
            return resp
        return error_response("模型管理已迁移到提供商管理，请使用 /providers/models 接口")


    async def _web_providers_get(self):
        """GET {base}/providers：返回提供商列表。

        返回完整 api_key（WebUI 管理面板可查看/回填编辑框）+ api_key_masked（兼容旧前端）。
        use_reverse_proxy：novalai 是否走反代模式（GET /generate）。
        本端点经 _web_require_login 保护，仅 Dashboard 登录用户可访问，完整 key 不对外泄露。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            providers = []
            for p in self.providers:
                providers.append({
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "type": p.get("type", ""),
                    "api_key": str(p.get("api_key", "") or ""),
                    "api_key_masked": self._mask_api_key(p.get("api_key", "")),
                    "base_url": p.get("base_url", ""),
                    "user_url": str(p.get("user_url", "") or ""),
                    "use_reverse_proxy": bool(p.get("use_reverse_proxy", False)),
                    "insecure_ssl": bool(p.get("insecure_ssl", False)),
                    "openai_api_mode": str(p.get("openai_api_mode", "chat") or "chat"),
                    "enabled": bool(p.get("enabled", True)),
                    "model_count": len(p.get("models") or []),
                })
            return json_response({"providers": providers})
        except Exception as e:
            return error_response(str(e))


    async def _web_providers_post(self):
        """POST {base}/providers：新增提供商。body {name,type,api_key,base_url,use_reverse_proxy?}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            name = str(payload.get("name", "") or "").strip()
            if not name:
                return error_response("提供商名称不能为空")
            ptype = str(payload.get("type", "") or "").strip()
            if ptype not in ("gemini", "openai", "novalai"):
                return error_response("type 必须是 gemini/openai/novalai 之一")
            api_key = str(payload.get("api_key", "") or "").strip()
            if not api_key:
                return error_response("API Key 不能为空")
            base_url = str(payload.get("base_url", "") or "").strip()
            if not base_url:
                return error_response("BaseURL 不能为空")
            provider = {
                "id": self._new_provider_id(),
                "name": name,
                "type": ptype,
                "api_key": api_key,
                "base_url": base_url,
                "user_url": str(payload.get("user_url", "") or "").strip(),
                "use_reverse_proxy": bool(payload.get("use_reverse_proxy", False)),
                "insecure_ssl": bool(payload.get("insecure_ssl", False)),
                "openai_api_mode": ("images" if str(payload.get("openai_api_mode", "chat") or "chat").strip().lower() == "images" else "chat"),
                "enabled": True,
                "models": copy.deepcopy(PROVIDER_DEFAULT_MODELS.get(ptype, [])),
            }
            self.providers.append(provider)
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True, "id": provider["id"]})
        except Exception as e:
            return error_response(str(e))


    async def _web_providers_delete(self):
        """POST {base}/providers/delete：删除提供商（其下模型一并删除）。body {id}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("id", "") or "").strip()
            if not pid:
                return error_response("id 不能为空")
            before = len(self.providers)
            self.providers = [p for p in self.providers if p.get("id") != pid]
            if len(self.providers) == before:
                return error_response("未找到该提供商")
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True})
        except Exception as e:
            return error_response(str(e))


    async def _web_providers_save(self):
        """POST {base}/providers/save：更新提供商配置。body {id,name,api_key,base_url,use_reverse_proxy,enabled}。

        api_key 传空串表示不修改（保留原值）；name/base_url/enabled/use_reverse_proxy 可选。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("id", "") or "").strip()
            if not pid:
                return error_response("id 不能为空")
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            if "name" in payload:
                name = str(payload.get("name", "") or "").strip()
                if not name:
                    return error_response("提供商名称不能为空")
                target["name"] = name
            if "type" in payload:
                ptype = str(payload.get("type", "") or "").strip()
                if ptype not in ("gemini", "openai", "novalai"):
                    return error_response("type 必须是 gemini/openai/novalai 之一")
                target["type"] = ptype
            if "base_url" in payload:
                base_url = str(payload.get("base_url", "") or "").strip()
                if not base_url:
                    return error_response("BaseURL 不能为空")
                target["base_url"] = base_url
            if "user_url" in payload:
                user_url = str(payload.get("user_url", "") or "").strip()
                target["user_url"] = user_url
            if "api_key" in payload:
                api_key = str(payload.get("api_key", "") or "").strip()
                if api_key:  # 传空串表示不修改，保留原值
                    target["api_key"] = api_key
            if "use_reverse_proxy" in payload:
                target["use_reverse_proxy"] = bool(payload.get("use_reverse_proxy"))
            if "insecure_ssl" in payload:
                target["insecure_ssl"] = bool(payload.get("insecure_ssl"))
            if "openai_api_mode" in payload:
                mode = str(payload.get("openai_api_mode", "chat") or "chat").strip().lower()
                if mode not in ("chat", "images"):
                    return error_response("openai_api_mode 必须是 chat 或 images 之一")
                target["openai_api_mode"] = mode
            if "enabled" in payload:
                target["enabled"] = bool(payload.get("enabled"))
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True})
        except Exception as e:
            return error_response(str(e))


    async def _web_providers_models_get(self):
        """GET {base}/providers/models?provider={id}：返回该提供商模型数组。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            pid = (request.query.get("provider", "") or "").strip()
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            models = [
                {"name": m["name"], "quota": int(m.get("quota", 1)), "desc": str(m.get("desc", "") or ""),
                 "params": dict(m.get("params") or {}),
                 "allowed_sizes": m.get("allowed_sizes")}
                for m in (target.get("models") or [])
                if isinstance(m, dict) and m.get("name")
            ]
            return json_response({"models": models})
        except Exception as e:
            return error_response(str(e))


    async def _web_providers_models_post(self):
        """POST {base}/providers/models：保存该提供商模型。body {provider:id, models:[...]}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("provider", "") or "").strip()
            if not pid:
                return error_response("provider 不能为空")
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            models = payload.get("models")
            if not isinstance(models, list):
                return error_response("models 必须是数组")
            # 采用纯 JSON 对象层清洗校验，杜绝分号序列化截断与反序列化解析失败
            normalized_models, err = self._validate_and_normalize_models(models)
            if err:
                return error_response(err)
            target["models"] = normalized_models
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True, "count": len(target["models"])})
        except Exception as e:
            return error_response(str(e))


    async def _web_users_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            search = (request.query.get("search", "") or "").strip()
            page = max(1, request.query.get("page", 1, type=int))
            page_size = max(1, min(request.query.get("page_size", 20, type=int), WEB_PAGE_SIZE_MAX))
            items = [{"qq": uid, "count": cnt} for uid, cnt in self.user_counts.items()]
            if search:
                items = [it for it in items if search in it["qq"]]
            items.sort(key=lambda x: x["count"], reverse=True)
            total = len(items)
            start = (page - 1) * page_size
            return json_response({"users": items[start:start + page_size], "total": total})
        except Exception as e:
            return error_response(str(e))


    async def _web_users_add(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            qq = str(payload.get("qq", "")).strip()
            if not qq:
                return error_response("qq 不能为空")
            try:
                amount = int(payload.get("amount", 0))
            except (TypeError, ValueError):
                return error_response("amount 必须为整数")
            if amount < 1:
                return error_response("amount 必须 >= 1")
            async with self._counts_lock:
                current_count = self._get_user_count(qq)
                new_count = current_count + amount
                # 管理员豁免 cap——不受 user_count_cap 截断
                self.user_counts[str(qq)] = new_count
                await self._save_user_counts()
            return json_response({"ok": True, "count": new_count})
        except Exception as e:
            return error_response(str(e))


    async def _web_groups_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            search = (request.query.get("search", "") or "").strip()
            page = max(1, request.query.get("page", 1, type=int))
            page_size = max(1, min(request.query.get("page_size", 20, type=int), WEB_PAGE_SIZE_MAX))
            items = [{"group_id": gid, "count": cnt} for gid, cnt in self.group_counts.items()]
            if search:
                items = [it for it in items if search in it["group_id"]]
            items.sort(key=lambda x: x["count"], reverse=True)
            total = len(items)
            start = (page - 1) * page_size
            return json_response({"groups": items[start:start + page_size], "total": total})
        except Exception as e:
            return error_response(str(e))


    async def _web_groups_add(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "")).strip()
            if not group_id:
                return error_response("group_id 不能为空")
            try:
                amount = int(payload.get("amount", 0))
            except (TypeError, ValueError):
                return error_response("amount 必须为整数")
            if amount < 1:
                return error_response("amount 必须 >= 1")
            async with self._counts_lock:
                current_count = self._get_group_count(group_id)
                new_count = current_count + amount
                self.group_counts[str(group_id)] = new_count
                await self._save_group_counts()
            return json_response({"ok": True, "count": new_count})
        except Exception as e:
            return error_response(str(e))


    async def _web_users_set(self):
        """设置用户次数为指定值（0 或任意非负整数，不受 user_count_cap 上限限制）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            qq = str(payload.get("qq", "")).strip()
            if not qq:
                return error_response("qq 不能为空")
            try:
                count = int(payload.get("count", 0))
            except (TypeError, ValueError):
                return error_response("count 必须为整数")
            if count < 0:
                return error_response("count 必须 >= 0")
            async with self._counts_lock:
                self.user_counts[str(qq)] = count
                await self._save_user_counts()
            return json_response({"ok": True, "count": count})
        except Exception as e:
            return error_response(str(e))


    async def _web_groups_set(self):
        """设置群组次数为指定值（0 或任意非负整数）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "")).strip()
            if not group_id:
                return error_response("group_id 不能为空")
            try:
                count = int(payload.get("count", 0))
            except (TypeError, ValueError):
                return error_response("count 必须为整数")
            if count < 0:
                return error_response("count 必须 >= 0")
            async with self._counts_lock:
                self.group_counts[str(group_id)] = count
                await self._save_group_counts()
            return json_response({"ok": True, "count": count})
        except Exception as e:
            return error_response(str(e))


    async def _web_logs_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            scope = (request.query.get("scope", "") or "").strip() or "本群"
            days = request.query.get("days", 0, type=int)
            limit = max(1, min(request.query.get("limit", 20, type=int), 200))
            page = max(1, request.query.get("page", 1, type=int))
            records = list(reversed(self.generation_history))  # 时间倒序（最新在前）
            if scope != "全局":
                # 本群：WebUI 无群上下文，需显式 group_id 查询参数
                gid = (request.query.get("group_id", "") or "").strip()
                records = [r for r in records if str(r.get("group_id", "")) == str(gid)] if gid else []
            if days > 0:
                cutoff = datetime.now() - timedelta(days=days)
                filtered = []
                for r in records:
                    try:
                        if datetime.strptime(str(r.get("timestamp", "")), "%Y%m%d_%H%M%S_%f") >= cutoff:
                            filtered.append(r)
                    except ValueError:
                        continue
                records = filtered
            total = len(records)
            start = (page - 1) * limit
            logs = [{
                "timestamp": r.get("timestamp", ""),
                "group_id": r.get("group_id", ""),
                "user_id": r.get("user_id", ""),
                "prompt": r.get("prompt", ""),
                "model": r.get("model", ""),
                "file_path": r.get("file_path", ""),
            } for r in records[start:start + limit]]
            return json_response({"logs": logs, "total": total})
        except Exception as e:
            return error_response(str(e))


    async def _web_logs_delete(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            # 用 _history_lock 包裹「读-筛选-改-盘」，与 _append_generation_history 一致，
            # 防止删除期间并发生成追加导致 read-modify-write 丢记录
            async with self._history_lock:
                remaining = list(self.generation_history)
                if "timestamps" in payload:
                    ts_set = set(str(t) for t in payload.get("timestamps", []) if str(t).strip())
                    if not ts_set:
                        return error_response("timestamps 不能为空")
                    remaining = [r for r in remaining if str(r.get("timestamp", "")) not in ts_set]
                elif "start" in payload and "end" in payload:
                    start_ts = str(payload.get("start", "")).strip()
                    end_ts = str(payload.get("end", "")).strip()
                    if not start_ts or not end_ts:
                        return error_response("范围删除需要 start 与 end 时间戳")
                    scope_key = str(payload.get("scope", "全局") or "全局")
                    kept = []
                    for r in self.generation_history:
                        r_ts = str(r.get("timestamp", ""))
                        in_range = start_ts <= r_ts <= end_ts
                        in_scope = (scope_key == "全局") or (str(r.get("group_id", "")) == str(scope_key))
                        if not (in_range and in_scope):
                            kept.append(r)
                    remaining = kept
                else:
                    return error_response("需要提供 timestamps 或 scope+start+end")
                removed = len(self.generation_history) - len(remaining)
                deleted_records = [r for r in self.generation_history if r not in remaining]
                if removed:
                    self.generation_history = remaining
                    await self._save_generation_history()
            if deleted_records:
                self._delete_generation_files(deleted_records)
            return json_response({"ok": True, "deleted": removed})
        except Exception as e:
            return error_response(str(e))


    async def _web_logs_image(self):
        """返回生图记录对应的图片（base64 data URL）。

        参数 path: 日志里的 file_path（服务器绝对路径）。
        安全校验：resolve() 后必须位于 StarTools.get_data_dir()/generations 目录之内，防路径穿越。
        说明：父页面 bridge api:get 走 axios 默认 JSON 解析，二进制 file_response 无法直接透传，
        故改为返回 JSON {"image": "data:<type>;base64,..."}，前端以 <img src=dataURL> 展示。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            raw_path = (request.query.get("path", "") or "").strip()
            resolved, content_type, err_reason = self._resolve_generation_file(raw_path)
            if not resolved:
                if err_reason == "文件不存在" or err_reason == "文件读取失败":
                    status = 404
                elif err_reason and "过大" in err_reason:
                    status = 413
                else:
                    status = 400
                return error_response(err_reason, status)
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, resolved.read_bytes)
            b64 = base64.b64encode(data).decode("ascii")
            return json_response({"image": f"data:{content_type};base64,{b64}"})
        except Exception as e:
            logger.error(f"[NewImage] 读取生图图片失败: {e}", exc_info=True)
            return error_response(str(e))


    async def _web_artist_presets_get(self):
        """GET {base}/artist-presets：返回画师串预设库。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({"presets": self.artist_presets})
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_presets_post(self):
        """POST {base}/artist-presets：保存画师串预设库。body {presets:[{name,artists,negative}]}。

        name/artists 非空（negative 可空）；name 去重保序（重复取最后一个）。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            presets = payload.get("presets")
            if not isinstance(presets, list):
                return error_response("presets 必须是数组")
            cleaned: List[Dict[str, Any]] = []
            seen: set = set()
            for item in presets:
                if not isinstance(item, dict):
                    return error_response("presets 元素必须是对象")
                name = str(item.get("name", "") or "").strip()
                if not name:
                    return error_response("预设名不能为空")
                artists = str(item.get("artists", "") or "").strip()
                if not artists:
                    return error_response(f"预设 [{name}] 的画师串不能为空")
                negative = str(item.get("negative", "") or "").strip()
                if name in seen:
                    # 重名：覆盖同名条目（保序）
                    for existing in cleaned:
                        if existing["name"] == name:
                            existing["artists"] = artists
                            existing["negative"] = negative
                            break
                    continue
                seen.add(name)
                cleaned.append({"name": name, "artists": artists, "negative": negative})
            self.artist_presets = cleaned
            await self._save_artist_presets()
            await self._load_artist_presets()
            return json_response({"ok": True, "count": len(self.artist_presets)})
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_presets_export(self):
        """GET {base}/artist-presets/export：导出画师串预设库为标准分享 JSON。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = {
                "format": "rainnya_artist_presets",
                "version": "1.2.0",
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(self.artist_presets),
                "presets": self.artist_presets,
            }
            return json_response(payload)
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_presets_import(self):
        """POST {base}/artist-presets/import：智能导入画师预设（多模自适应解析）。body {data:Any, mode:'merge'|'overwrite'}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            raw_data = payload.get("data")
            if raw_data is None:
                return error_response("缺少导入数据 (data 字段)")
            # 支持传入 JSON 字符串时内部自动 json.loads
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except Exception as je:
                    return error_response(f"传入的文本非合法 JSON: {je}")
            mode = str(payload.get("mode", "merge") or "merge").strip().lower()
            try:
                new_presets, source_name = self._parse_imported_artist_presets(raw_data)
            except ValueError as ve:
                return error_response(str(ve))

            if mode == "overwrite":
                self.artist_presets = new_presets
            else:
                # merge 模式：已有同名更新，无则追加保序
                existing_map = {p["name"]: p for p in self.artist_presets}
                for np in new_presets:
                    if np["name"] in existing_map:
                        existing_map[np["name"]]["artists"] = np["artists"]
                        if np["negative"]:
                            existing_map[np["name"]]["negative"] = np["negative"]
                    else:
                        self.artist_presets.append(np)
                        existing_map[np["name"]] = np

            await self._save_artist_presets()
            await self._load_artist_presets()
            return json_response({
                "ok": True,
                "imported_count": len(new_presets),
                "total_count": len(self.artist_presets),
                "source_type": source_name,
                "mode": mode,
            })
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_presets_detect_nai_brush(self):
        """GET {base}/artist-presets/detect-nai-brush：探测本地已安装的 AstrBot 幻笔插件配置。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            # 候选检测路径
            candidates = [
                self.plugin_data_dir.parent / "astrbot_plugin_nai_brush",
                self.plugin_data_dir.parent.parent / "plugins" / "astrbot_plugin_nai_brush",
                self.plugin_data_dir.parent.parent / "data" / "plugins" / "astrbot_plugin_nai_brush",
                Path("/home/miaomiao/Project/astrbot/data/plugins/astrbot_plugin_nai_brush"),
                Path("/home/miaomiao/Project/astrbot/data/plugin_data/astrbot_plugin_nai_brush"),
                Path("/tmp/astrbot_plugin_nai_brush"),
            ]
            found_info = None
            for p in candidates:
                if not p.exists():
                    continue
                # 检查 config.json
                for fn in ("config.json", "_conf_schema.json"):
                    cfg_file = p / fn
                    if cfg_file.is_file():
                        try:
                            content = cfg_file.read_text(encoding="utf-8")
                            data = json.loads(content)
                            new_presets, src = self._parse_imported_artist_presets(data)
                            if new_presets:
                                found_info = {
                                    "found": True,
                                    "path": str(cfg_file),
                                    "count": len(new_presets),
                                    "source_type": src,
                                    "presets": new_presets,
                                }
                                break
                        except Exception:
                            continue
                if found_info:
                    break

            if found_info:
                return json_response(found_info)
            return json_response({"found": False})
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_groups_get(self):
        """GET {base}/artist-presets/group：返回各群画师串激活映射。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({"groups": self.group_artists})
        except Exception as e:
            return error_response(str(e))


    async def _web_artist_groups_post(self):
        """POST {base}/artist-presets/group：设置某群画师串预设。body {group_id, preset}。

        preset 为空串或 "off" 表示关闭（移除该群配置）；否则须在预设库中存在。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "") or "").strip()
            if not group_id:
                return error_response("group_id 不能为空")
            preset = str(payload.get("preset", "") or "").strip()
            if not preset or preset.lower() == "off":
                self.group_artists.pop(group_id, None)
                await self._save_group_artists()
                return json_response({"ok": True, "preset": ""})
            if not any(p.get("name") == preset for p in self.artist_presets):
                return error_response(f"预设 [{preset}] 不存在")
            self.group_artists[group_id] = preset
            await self._save_group_artists()
            return json_response({"ok": True, "preset": preset})
        except Exception as e:
            return error_response(str(e))


    async def _web_config_get(self):
        """GET {base}/config：返回全部设置（默认值对齐 _conf_schema.json）。

        注意：api_url/api_keys/is_gemini_api/model/provider_id 已迁移到提供商管理（providers.json），
        不再从此接口返回；WebUI 基础设置页暴露全部非 API 配置字段（含各渠道默认参数）。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({
                # 基础
                "prefix": bool(self.conf.get("prefix", True)),
                "extra_prefix": str(self.conf.get("extra_prefix", "bnn") or ""),
                "selfie_prompt": str(self.conf.get("selfie_prompt", "") or ""),
                "use_proxy": bool(self.conf.get("use_proxy", False)),
                "proxy_url": str(self.conf.get("proxy_url", "") or ""),
                "download_timeout": self.conf.get("download_timeout", 60),
                "download_retries": self.conf.get("download_retries", 3),
                "api_timeout": self.conf.get("api_timeout", 120),
                # Gemini
                "safety_level": str(self.conf.get("safety_level", "") or ""),
                "image_size": str(self.conf.get("image_size", "") or ""),
                "hires_whitelist": self.conf.get("hires_whitelist", []),
                # OpenAI 渠道默认参数
                "openai_size": str(self.conf.get("openai_size", "") or ""),
                "openai_temperature": self.conf.get("openai_temperature", 1.0),
                "openai_max_tokens": self.conf.get("openai_max_tokens", 2048),
                # NovalAI 渠道默认参数
                "novalai_size": str(self.conf.get("novalai_size", "") or ""),
                "novalai_sampler": str(self.conf.get("novalai_sampler", "k_euler_ancestral") or ""),
                "novalai_steps": self.conf.get("novalai_steps", 23),
                "novalai_scale": self.conf.get("novalai_scale", 6.0),
                "novalai_negative_prompt": str(self.conf.get("novalai_negative_prompt", "") or ""),
                "novalai_429_retry_enabled": bool(self.conf.get("novalai_429_retry_enabled", True)),
                "novalai_429_max_retries": self.conf.get("novalai_429_max_retries", 10),
                "novalai_429_retry_delay": self.conf.get("novalai_429_retry_delay", 5),
                # 权限
                "user_whitelist": self.conf.get("user_whitelist", []),
                "user_blacklist": self.conf.get("user_blacklist", []),
                "group_whitelist": self.conf.get("group_whitelist", []),
                "group_blacklist": self.conf.get("group_blacklist", []),
                # 次数
                "enable_user_limit": bool(self.conf.get("enable_user_limit", True)),
                "enable_group_limit": bool(self.conf.get("enable_group_limit", False)),
                "user_count_cap": self.conf.get("user_count_cap", 0),
                "group_task_limit": self.conf.get("group_task_limit", 2),
                # 签到
                "enable_checkin": bool(self.conf.get("enable_checkin", False)),
                "checkin_fixed_reward": self.conf.get("checkin_fixed_reward", 3),
                "enable_random_checkin": bool(self.conf.get("enable_random_checkin", False)),
                "checkin_random_reward_min": self.conf.get("checkin_random_reward_min", 1),
                "checkin_random_reward_max": self.conf.get("checkin_random_reward_max", 5),
                # 帮助/预设
                "help_menu": str(self.conf.get("help_menu", "") or ""),
                "prompt_list": self.conf.get("prompt_list", []),
                # 打标
                "tagger_provider_id": str(self.conf.get("tagger_provider_id", "") or ""),
                "tagger_temperature": self.conf.get("tagger_temperature", 0.2),
                "tagger_max_tokens": self.conf.get("tagger_max_tokens", 900),
                # 画师推荐
                "artist_recommend_enabled": bool(self.conf.get("artist_recommend_enabled", False)),
                "artist_recommend_max_candidates": self.conf.get("artist_recommend_max_candidates", 10),
                "artist_recommend_min_match": self.conf.get("artist_recommend_min_match", 3),
                # Danbooru 动态搜索
                "danbooru_enabled": bool(self.conf.get("danbooru_enabled", False)),
                "danbooru_base_url": str(self.conf.get("danbooru_base_url", "") or ""),
                "danbooru_limit": self.conf.get("danbooru_limit", 10),
                "danbooru_min_posts": self.conf.get("danbooru_min_posts", 100),
                # NovelAI token 预检
                "novai_precheck_enabled": bool(self.conf.get("novai_precheck_enabled", True)),
                "novai_precheck_timeout": self.conf.get("novai_precheck_timeout", 10),
                "novai_point_warn_threshold": self.conf.get("novai_point_warn_threshold", 10),
                # 自动撤回
                "auto_recall_enabled": bool(self.conf.get("auto_recall_enabled", False)),
                "auto_recall_delay": self.conf.get("auto_recall_delay", 5),
                "auto_recall_allowed_groups": self.conf.get("auto_recall_allowed_groups", []),
                # NSFW 过滤
                "nsfw_filter_enabled": bool(self.conf.get("nsfw_filter_enabled", False)),
                "nsfw_filter_tags": str(self.conf.get("nsfw_filter_tags", "") or ""),
                # NSFW 加密
                "nsfw_zip_enabled": bool(self.conf.get("nsfw_zip_enabled", True)),
                # LLM 工具系统提示词注入
                "enable_llm_tool_system_prompt": bool(self.conf.get("enable_llm_tool_system_prompt", True)),
                "custom_llm_tool_system_prompt": str(self.conf.get("custom_llm_tool_system_prompt", "") or ""),
                # 智力矫正
                "force_full_params": bool(self.conf.get("force_full_params", False)),
                "force_english_tags": bool(self.conf.get("force_english_tags", False)),
                "correction_reject_template": str(self.conf.get("correction_reject_template", "") or ""),
            })
        except Exception as e:
            return error_response(str(e))


    async def _web_config_post(self):
        """POST {base}/config：保存全部设置（白名单 _WEB_CONFIG_FIELD_TYPES，一次性原子落盘）。

        兼容旧前端：`blacklist` 别名仍接受（映射到 user_blacklist）。
        prompt_list 元素必须是字符串且含 ":"（触发词:提示词），非法元素直接报错不落盘。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            # blacklist 别名 → conf 键 user_blacklist
            if "blacklist" in payload and "user_blacklist" not in payload:
                payload["user_blacklist"] = payload.pop("blacklist")
            saved = []
            field_labels = {
                "prefix": "要求指令带前缀", "extra_prefix": "自定义前缀", "use_proxy": "使用代理",
                "proxy_url": "代理地址", "download_timeout": "下载超时", "download_retries": "下载重试次数",
                "api_timeout": "API 超时", "openai_size": "OpenAI 默认分辨率", "openai_temperature": "OpenAI 温度",
                "openai_max_tokens": "OpenAI Max Tokens", "safety_level": "Gemini 安全过滤等级",
                "image_size": "Gemini 全局默认分辨率", "hires_whitelist": "高分辨率白名单",
                "novalai_size": "NovelAI 默认分辨率", "novalai_sampler": "NovelAI 采样器",
                "novalai_steps": "NovelAI 采样步数", "novalai_scale": "NovelAI 引导强度 scale",
                "novalai_negative_prompt": "NovelAI 负面提示词",
                "novalai_429_retry_enabled": "NovelAI 启用 429 自动重试",
                "novalai_429_max_retries": "NovelAI 429 最大重试次数",
                "novalai_429_retry_delay": "NovelAI 429 重试等待间隔",
                "novai_precheck_enabled": "NovelAI 启用预检",
                "novai_precheck_timeout": "NovelAI 预检超时", "novai_point_warn_threshold": "NovelAI 点数不足提醒阈值",
                "auto_recall_enabled": "启用自动撤回", "auto_recall_delay": "自动撤回延迟",
                "auto_recall_allowed_groups": "自动撤回生效群", "nsfw_filter_enabled": "启用 NSFW 过滤",
                "nsfw_filter_tags": "NSFW 标签", "nsfw_zip_enabled": "启用 NSFW 加密 ZIP 发送",
                "tagger_provider_id": "打标提供商 ID", "tagger_temperature": "打标温度",
                "tagger_max_tokens": "打标 Max Tokens", "artist_recommend_enabled": "启用画师推荐",
                "artist_recommend_max_candidates": "画师推荐最大候选数", "artist_recommend_min_match": "最少匹配标签数",
                "danbooru_enabled": "启用 Danbooru 动态搜索", "danbooru_base_url": "Danbooru API 地址",
                "danbooru_limit": "Danbooru 候选数", "danbooru_min_posts": "Danbooru 最少帖子数",
                "selfie_prompt": "自拍人物外貌特征", "user_whitelist": "用户白名单", "user_blacklist": "用户黑名单",
                "group_whitelist": "群白名单", "group_blacklist": "群黑名单", "enable_user_limit": "启用用户次数限制",
                "enable_group_limit": "启用群组次数限制", "user_count_cap": "用户持有次数上限",
                "group_task_limit": "群并发生成任务数", "enable_checkin": "开启每日签到",
                "checkin_fixed_reward": "签到固定奖励次数", "enable_random_checkin": "启用随机奖励",
                "checkin_random_reward_min": "随机奖励下限", "checkin_random_reward_max": "随机奖励上限",
                "help_menu": "帮助菜单", "prompt_list": "预设 prompt",
                "enable_llm_tool_system_prompt": "启用生图工具系统提示词注入",
                "custom_llm_tool_system_prompt": "自定义生图工具系统提示词",
                "force_full_params": "智力矫正：强制参数完整",
                "force_english_tags": "智力矫正：强制纯英文 Tag",
                "correction_reject_template": "智力矫正：打回提示词模板",
            }
            # AstrBotConfig(dict) 无 set 方法：先收集更新项，最后一次性原子落盘（避免多次写盘）
            config_updates: Dict[str, Any] = {}
            for key, expected in self._WEB_CONFIG_FIELD_TYPES.items():
                if key not in payload:
                    continue
                value = payload[key]
                label = field_labels.get(key, key)
                if expected is bool:
                    if not isinstance(value, bool):
                        return error_response(f"{label}：必须为布尔值 (true/false)")
                elif expected is float:
                    # float 字段：openai_temperature(0-2) / novalai_scale(0-10)，越界夹取（同 _coerce clamp 语义）
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        return error_response(f"{label}：请输入有效数字")
                    if key == "openai_temperature":
                        value = max(0.0, min(value, 2.0))
                    elif key == "novalai_scale":
                        value = max(0.0, min(value, 10.0))
                elif expected is int:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        return error_response(f"{label}：请输入有效整数")
                    # 非负校验：全部 int 字段均不允许负数（含超时/重试/签到奖励/上限等）
                    if value < 0:
                        return error_response(f"{label}：数值不能小于 0")
                    # 渠道字段范围夹取
                    if key == "openai_max_tokens":
                        value = max(64, min(value, 16384))
                    elif key == "novalai_steps":
                        value = max(1, min(value, 50))
                    elif key == "novalai_429_max_retries":
                        value = max(1, min(value, 50))
                    elif key == "novalai_429_retry_delay":
                        value = max(1, min(value, 60))
                elif expected is list:
                    if not isinstance(value, list):
                        return error_response(f"{label}：格式必须为列表")
                    if key == "prompt_list":
                        # 预设列表：前端按换行切分会把跨行提示词切成多个数组元素（无冒号碎片）
                        # → 后端先容错重组（无冒号元素并入前一个含冒号元素），重组后仍无冒号才报错
                        value = _rejoin_prompt_list(value)
                        for item in value:
                            if not isinstance(item, str) or ":" not in item:
                                return error_response("预设 prompt 格式错误：每条格式必须为「触发词:提示词」")
                    value = [str(v) for v in value]
                elif expected is str:
                    if not isinstance(value, str):
                        return error_response(f"{label}：必须为文本内容")
                    # 渠道 size 字段：openai 仅 1K/2K/4K；novalai 接受新档位键 + 旧值，非法回退默认
                    if key == "openai_size" and value.strip() not in ("", "1K", "2K", "4K"):
                        return error_response("OpenAI 默认分辨率：只能选择 1K、2K、4K 或不设置")
                    elif key == "novalai_size":
                        v = value.strip()
                        if v and v not in NOVALAI_SIZE_DIMENSIONS:
                            value = NOVALAI_DEFAULT_SIZE  # 非法值回退默认
                config_updates[key] = value
                saved.append(key)
            if config_updates:
                await self.conf.save_config_async(config_updates)
            # 重载受影响的运行时状态
            if "user_count_cap" in saved:
                try:
                    self.user_count_cap = max(0, int(self.conf.get("user_count_cap", 0)))
                except (TypeError, ValueError):
                    self.user_count_cap = 0
            if "group_task_limit" in saved:
                try:
                    self.group_task_limit = max(0, int(self.conf.get("group_task_limit", 2)))
                except (TypeError, ValueError):
                    self.group_task_limit = 0
                # 并发任务计数在限制变化后不再准确，清空让各群重新计数
                self.group_task_counts.clear()
            if "api_timeout" in saved:
                try:
                    self.api_timeout = max(API_TIMEOUT_MIN_SECONDS, int(self.conf.get("api_timeout", 120)))
                except (TypeError, ValueError):
                    self.api_timeout = 120
            if "safety_level" in saved:
                self.safety_level = (self.conf.get("safety_level", "") or "").strip()
            if "image_size" in saved:
                self.default_image_size = (self.conf.get("image_size", "") or "").strip().upper()
            if "hires_whitelist" in saved:
                self.hires_whitelist = [str(x) for x in self.conf.get("hires_whitelist", [])]
            # 预设（prompt_list）改动后即时重载 prompt_map，避免重启才生效
            if "prompt_list" in saved:
                await self._load_prompt_map()
            return json_response({"ok": True, "saved": saved})
        except Exception as e:
            return error_response(str(e))


    async def _web_workbench_models(self):
        """GET {base}/workbench/models：返回供测试台使用的所有模型与所属提供商上下文信息。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            items = []
            for m in self.available_models:
                p = self._find_usable_provider_for_model(m["name"]) or self._find_provider_for_model(m["name"])
                items.append({
                    "name": m["name"],
                    "quota": m.get("quota", 1),
                    "desc": m.get("desc", ""),
                    "params": m.get("params") or {},
                    "allowed_sizes": m.get("allowed_sizes"),
                    "provider_id": m.get("provider_id", ""),
                    "provider_name": p.get("name", "") if p else "",
                    "provider_type": p.get("type", "openai") if p else "openai",
                })
            return json_response({"models": items})
        except Exception as e:
            return error_response(str(e))


    async def _web_workbench_generate(self):
        """POST {base}/workbench/generate：测试台出图接口（支持多提供商特殊参数与参考图，自动存盘日志）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            prompt = str(payload.get("prompt", "") or "").strip()
            if not prompt:
                return error_response("提示词 (prompt) 不能为空")

            model_name = str(payload.get("model", "") or "").strip()
            image_size = str(payload.get("image_size", "") or "").strip() or None
            negative_prompt = str(payload.get("negative_prompt", "") or "").strip()
            custom_params = payload.get("params")
            gen_params = dict(custom_params) if isinstance(custom_params, dict) else {}

            if negative_prompt:
                gen_params["negative_prompt"] = negative_prompt
            if image_size:
                gen_params["size"] = image_size

            # 参考图（图生图测试）
            image_bytes_list: List[bytes] = []
            ref_b64 = payload.get("reference_image")
            if ref_b64 and isinstance(ref_b64, str):
                if ref_b64.startswith("data:"):
                    ref_b64 = ref_b64.split(",", 1)[1]
                try:
                    image_bytes_list.append(base64.b64decode(ref_b64))
                except Exception as e:
                    return error_response(f"参考图片 Base64 解码失败: {e}")

            start_time = datetime.now()
            res, used_model = await self._call_api(
                image_bytes_list, prompt,
                image_size=image_size,
                group_id=None,
                model_override=model_name or None,
                gen_params=gen_params,
                size_strict=False
            )
            elapsed = (datetime.now() - start_time).total_seconds()

            if isinstance(res, bytes):
                # 自动存盘并追加到日志时间线
                try:
                    await self._save_generation("webui", "admin", prompt, used_model, res)
                except Exception as e:
                    logger.warning(f"[NewImage/Workbench] 测试台生图存盘失败: {e}")

                img_b64 = f"data:image/png;base64,{base64.b64encode(res).decode('utf-8')}"
                return json_response({
                    "ok": True,
                    "model": used_model,
                    "elapsed": round(elapsed, 2),
                    "image_base64": img_b64,
                    "size_bytes": len(res),
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            else:
                return error_response(f"生图失败 ({round(elapsed, 2)}s)：{res}")
        except Exception as e:
            return error_response(str(e))

