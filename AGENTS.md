# AGENTS.md — astrbot_plugin_rainnya_image

AstrBot 生图插件 fork（自用增强版）。本文只记录 agent 容易判断错误的事实。

## 当前状态

- 当前分支：`main`；主要自用远端为 Gitea `origin`，另有 GitHub `github` 与上游 `upstream`。
- 插件元数据在 `metadata.yaml`：名称 `astrbot_plugin_rainnya_image`，版本 `v1.2.6`。
- 没有本地真实生图测试；真实验证需要 AstrBot 运行时和有效 API key。

## 结构（v1.2.5 起已模块化，main.py 仅保留装配）

- `main.py`：**仅装配**——`@register` 主类、Mixin 继承列表、`__init__`/`initialize`/`terminate`。
- `constants.py`：全部常量、Prompt 模板、`PARAM_SCHEMA`、无状态工具函数（`__all__` 显式导出，含下划线名）。
- `providers/`：生图渠道驱动 Mixin。`base.py`=`_call_api` 路由核心，`gemini.py` / `openai.py` / `novalai.py` 各自渠道。
- `services/`：领域服务 Mixin。`state.py`（加载/持久化/模型解析）、`quota.py`、`artist.py`、`media.py`、`interactive.py`。
- `handlers/`：AstrBot 接口层 Mixin。`commands.py`（群指令）、`llm_tools.py`（LLM 工具）、`web_api.py`（WebUI 后端）。
- `image_parser.py`：解析响应图片；`image_workflow.py`：获取/下载/处理图片；`storage.py`：异步 JSON 原子持久化。
- `pages/manage/`：原生 HTML/CSS/JS WebUI，通过 AstrBot bridge SDK 调用 API。
- `_conf_schema.json`：配置 schema；`README.md`：用户文档；`release/`：源码包产物。

**新增功能放哪**：新渠道 → `providers/` 加文件；新指令 → `handlers/commands.py`；新 LLM 工具 → `handlers/llm_tools.py`；新 Web API → `handlers/web_api.py`；新持久化/业务逻辑 → `services/`。

**打包注意**：`providers/`、`services/`、`handlers/`、`constants.py` 必须打进 zip，漏了会启动报 ImportError。

## 校验命令

```bash
# 全模块语法编译（含 Mixin 子模块）
python -m py_compile main.py constants.py image_parser.py image_workflow.py storage.py \
  providers/*.py services/*.py handlers/*.py
python -m json.tool _conf_schema.json >/dev/null
node --check pages/manage/app.js
```

# 真实导入验证（用 AstrBot 的 venv，能验证 Mixin 装配与装饰器注册）
PYTHONPATH=/home/miaomiao/Project/astrbot:/home/miaomiao/Project/Astrbotplugins \
  /home/miaomiao/Project/astrbot/.venv/bin/python -c "import astrbot_plugin_nova_newimage.main as M; print(len(M.FigurineProPlugin.__mro__))"

无法在本仓库跑真实生图测试（需 AstrBot 运行时 + API key）。

## 架构关键点（易搞错）

- **三类路由**：Gemini 原生、OpenAI 兼容、NovelAI 原生/反代；改 API 必须覆盖对应 provider 分支。
- **OpenAI 兼容有第三种子模式**：`provider["openai_api_mode"]` = `chat`（/v1/chat/completions）/ `images`（/v1/images/generations 文生图 + 自动路由 /v1/images/edits 图生图）。纯图像模型（muse-image、dall-e-3、gpt-image-1）必须用 images。
- **⚠️ Mixin 拆分的致命坑（v1.2.5 踩过，v1.2.6 修复）**：
  - 装饰器**注册**是定义时完成（子模块没问题），但框架**实例绑定**用的是**模块路径严格相等**：
    `handler.handler_module_path == metadata.module_path`（`get_handlers_by_module_name` 与 LLM 工具循环都是 `==`，不是前缀前缀匹配）。
  - `metadata.module_path` 恒为入口模块（`...main`），子模块方法的 `__module__` 是子模块路径 → **不相等 → 框架跳过绑定** → 运行时 `self`/`event` 错位，报
    `LLMToolsMixin.generate_image() missing 1 required positional argument: 'event'` 或 `CommandsMixin.xxx() missing ... 'self'`。
  - 解决：`FigurineProPlugin._ensure_runtime_bindings()`（`__init__` 末尾调用）显式把本插件 `functools.partial(raw, self)` 补绑，并把 `handler_module_path` 归一到入口模块。
  - **动结构前必读**：若未来再往子模块搬带装饰器的方法，确认该 shim 仍然覆盖（它按包前缀扫描，自动覆盖新子模块，无需改代码）。
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
