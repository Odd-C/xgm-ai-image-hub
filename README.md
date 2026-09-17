# AI Image Hub

[在线前端演示](https://odd-c.github.io/ai-image-hub/)（静态模拟数据，不调用付费 API）

公司部门内部使用的多平台 AI 生图工作台。第一阶段统一接入 Lovart、LibTV 和
OpenAI Images 兼容 API。系统将用户原始提示词直接提交给所选模型，不做提示词扩写。

## 已实现

- 部门账号登录、管理员创建/停用账号、用户数据隔离
- 一个账号可拥有多个项目，一个项目固定对应一张独立画布
- 项目创建、重命名、归档，以及项目级提示词、任务、素材和结果隔离
- 项目画布/草稿状态自动保存；旧版 SQLite 历史记录自动迁入保留项目
- 普通账号不显示也不能访问系统管理；管理员负责账号与平台状态
- Lovart、LibTV、OpenAI Images 兼容 API 三类执行器
- 模型能力驱动的平台、模型、比例和质量选择
- 最多 14 张参考图，校验格式、尺寸和文件大小
- 持久化任务队列、进程中断保护、有限的安全重试
- 每次提交不可变记录原始提示词、参数、参考图顺序和父任务关系
- CSRF 防护、登录尝试限速与生成提交幂等键
- 冻结每次任务的供应商模型能力快照
- 个人提示词历史搜索、平台/结果标记筛选、一键再次使用
- 成功结果的“满意 / 采用 / 不满意”三态标记
- 结果预览与下载、管理端任务和模型连接状态
- 前端不使用 CDN、外部字体、遥测或第三方脚本

## 启动

```bash
cp .env.example .env
# 必须修改 SESSION_SECRET、管理员密码，并填写实际平台凭据
uv sync --dev
uv run uvicorn image_hub.app:app --host 0.0.0.0 --port 5190
```

打开 `http://服务器地址:5190`。首次启动根据 `.env` 创建管理员账号。管理员密码只在
首次建库时使用；修改已有管理员密码需要通过管理脚本或数据库迁移完成。

也可以使用容器启动：

```bash
docker compose up -d --build
```

Compose 默认把两个 skill 目录只读挂载进容器；如果部署到其他服务器，需按实际路径调整两个
挂载。容器端口默认只绑定 `127.0.0.1`，应由公司内网反向代理提供 HTTPS；不要直接把 5190
端口开放到公网。

## 配置执行器

三个执行器都不需要额外安装可执行程序：

| 执行器 | 依赖 | 需要的配置 |
|---|---|---|
| LibTV | 官方 `libtv-skill` 的 `scripts/` 目录 | `IMAGE_HUB_LIBTV_SKILL_SCRIPTS`、`IMAGE_HUB_LIBTV_ACCESS_KEY` |
| Lovart | 官方 `lovart-api` 的 `agent_skill.py` | `IMAGE_HUB_LOVART_SKILL_SCRIPT`、`IMAGE_HUB_LOVART_ACCESS_KEY`／`SECRET_KEY` |
| API | 无 | `IMAGE_HUB_OPENAI_IMAGE_*` |

```env
IMAGE_HUB_LIBTV_SKILL_SCRIPTS=/opt/libtv-skill/scripts
IMAGE_HUB_LIBTV_ACCESS_KEY=your-access-key
IMAGE_HUB_LOVART_SKILL_SCRIPT=/opt/lovart-api/scripts/agent_skill.py
IMAGE_HUB_LOVART_ACCESS_KEY=ak_xxx
IMAGE_HUB_LOVART_SECRET_KEY=sk_xxx
```

这两组凭据与脚本路径也可以在「管理后台 → 平台与模型」里填写，管理员保存后即可用「测试连接」
验证（LibTV 会创建一次空会话，Lovart 只查询生成模式，都不会产生生成费用）。管理后台的取值
优先于环境变量，且只以 `0600` 私有文件保存在服务端。

skill 脚本由服务进程自己的 Python 解释器执行，因此 Windows 上不需要 `python3` 别名，也
不需要把 skill 装进 `PATH`。

## 配置图像 API

`IMAGE_HUB_OPENAI_IMAGE_MODELS` 使用逗号分隔，每项格式为 `模型ID|显示名称`：

```env
IMAGE_HUB_OPENAI_IMAGE_MODELS=doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro,gpt-image-1|GPT Image 1
```

这类上游需兼容 `POST /images/generations`。API Key 只保存在服务端环境变量中，不会发送
到浏览器。

## 数据目录

- SQLite：`data/image-hub.db`
- 参考图与结果：`storage/generations/<generation-id>/`

正式多人环境建议将数据库切换为 PostgreSQL，并把 `storage` 挂载到持久卷或内部对象存储。

## 验证

```bash
uv run ruff check src tests
uv run pytest -q
```

## 隐私边界

浏览器只访问部署本系统的同源服务。只有后端执行器会按管理员配置访问 Lovart、LibTV 或
图像 API 上游；页面没有 CDN、外部字体、图标服务、分析脚本或遥测。提示词和图片属于内部
数据，应通过内网、HTTPS、服务器访问控制和定期备份保护。
