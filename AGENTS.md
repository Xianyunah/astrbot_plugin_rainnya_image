# AGENTS.md — astrbot_plugin_nova_newimage

AstrBot 生图插件（单文件）。仅供 agent 避免踩坑的高信号事实。

## 结构

- `main.py`（1255 行）—— 全部逻辑，单个 `FigurineProPlugin(Star)` 类
- `_conf_schema.json` —— AstrBot 配置面板 schema（含 40+ 预设 prompt_list）
- `metadata.yaml` —— 插件元数据，`version: v1.4.1`（与 main.py @register 的版本号同步）
- `README.md` —— 功能/命令/更新日志文档

无 requirements.txt / 无构建系统 / 无测试。依赖 `aiohttp`、`Pillow` 由 AstrBot 运行时提供，改动只改 main.py。

## 校验命令

```bash
python -m py_compile main.py   # 唯一可行的本地校验（语法检查）
```

无法在本仓库跑真实生图测试（需 AstrBot 运行时 + API key）。

## 架构关键点（易搞错）

- **双路由**：`is_gemini_api` 开关决定走 `_call_gemini_api`（generateContent，支持 safetySettings/imageSize）还是 `_call_openai_api`（/v1/chat/completions，含 SSE 流式拼接）。改 API 逻辑必须同时考虑两条路由。
- **图像提取**：`_extract_image_bytes_from_response` 兼容 5+ 种响应结构（data[].url、message.images、content 内嵌 base64/markdown），下载失败回退返回 `FALLBACK_URL:` 字符串——调用方按 `isinstance(res, bytes)` vs `str` 分支判断成功/失败。
- **图片来源**：`ImageWorkflow.get_images` 依次从回复链、消息本体取 Image，无图时用 @用户头像（`q1.qlogo.cn`），再无则用发送者头像。bnn 模式无图也允许（纯 prompt 文生图）；预设模式无图直接拒绝。
- **次数扣减**：成功生成后扣次数（管理员豁免），群组优先于个人。数据持久化在 `StarTools.get_data_dir()` 下三个 json（user_counts / group_counts / user_checkin），不在仓库内。
- **provider 动态获取**：`_call_api` 里每次实时 `self.context.get_provider_by_id()`，与框架启动顺序解耦（勿缓存成实例态）。
- **@ 文本清理**：构建 prompt 前用正则 `@\S+?\(\d+\)` / `@\S+` 清除 @ 提及残留（v1.4.1 修复过，勿回退）。

## 用户约定

- 称呼用户「喵喵」；说话结尾带喵
- 版本发布走 Gitea/GitHub 流程：同步版本号（metadata.yaml + main.py @register）→ 提交推送 → tag → 源码包 release
- 本仓库远端是 GitHub `YumenoSayuri/astrbot_plugin_nova_newimage`
