# AGENTS.md — astrbot_plugin_rainnya_image

AstrBot 生图插件 fork（自用增强版）。本文只记录 agent 容易判断错误的事实。

## 当前状态

- 当前分支：`main`；主要自用远端为 Gitea `origin`，另有 GitHub `github` 与上游 `upstream`。
- 插件元数据在 `metadata.yaml`：名称 `astrbot_plugin_rainnya_image`，版本 `v1.2.0`。
- 没有本地真实生图测试；真实验证需要 AstrBot 运行时和有效 API key。

## 结构

- `main.py`：插件主逻辑、指令、LLM 工具、配额、提供商路由和 Web API。
- `image_parser.py`：解析响应图片；`image_workflow.py`：获取/下载/处理图片；`storage.py`：异步 JSON 原子持久化。
- `pages/manage/`：原生 HTML/CSS/JS WebUI，通过 AstrBot bridge SDK 调用 API。
- `_conf_schema.json`：配置 schema；`README.md`：用户文档；`release/`：源码包产物。

## 校验命令

```bash
python -m py_compile main.py image_parser.py image_workflow.py storage.py
python -m json.tool _conf_schema.json >/dev/null
```

无法在本仓库跑真实生图测试（需 AstrBot 运行时 + API key）。

## 架构关键点（易搞错）

- **三类路由**：Gemini 原生、OpenAI 兼容、NovelAI 原生/反代；改 API 必须覆盖对应 provider 分支。
- **图片提取**：调用方区分 `bytes`、`FALLBACK_URL:` 和 `None`，不要复制解析逻辑。
- **配额**：成功生成后才扣次数；管理员豁免；数据在 AstrBot 数据目录。
- **WebUI 名称必须一致**：metadata name、`WEB_PLUGIN_NAME`、页面路径均为 `astrbot_plugin_rainnya_image`；bridge SDK 要在 app.js 前加载。
- **配置保存**：`AstrBotConfig` 没有 `.set()`，使用 `self.conf[key] = value` + `await self.conf.save_config_async()`。
- **指令参数**：`event.message_str` 可能包含指令名；多词 prompt 手动剥离指令名。
- **异步任务**：后台任务保存于 `_bg_tasks`，`terminate()` 时回收；生成图和历史 JSON 使用现有 storage/日志路径。

## 用户约定

- 称呼用户「喵喵」；说话结尾带喵
- 版本发布走 Gitea/GitHub 流程：同步版本号（metadata.yaml + main.py @register）→ 提交推送 → tag → 源码包 release
- 完成功能后按全局约定用 `~/Project/astrbot/astrbot_chat.py` 让雨喵转达用户；不要回显凭据。
- 复杂任务先调研/设计，再实现和 oracle 审查；每 1–2 次改动尽快 commit。文档同步项目与 `/home/miaomiao/Project/闲聊/doc/`。
