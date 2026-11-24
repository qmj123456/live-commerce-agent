# 抖音直播间 Agent

Python + FastAPI + LangChain tools + LangGraph + Playwright CDP + React 管理面板。

## 安装与运行

在项目根目录 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
Copy-Item .env.example .env
cd web
npm install --cache ..\data\npm-cache
npm run build
cd ..
.\.venv\Scripts\python -m live_agent.main serve
```

控制台：http://127.0.0.1:8765 ，接口说明：http://127.0.0.1:8765/docs 。
默认不连接浏览器、不发送；点击“连接直播间”开始采集。

## 浏览器连接

双击根目录 `启动Chrome9333.cmd`，即可启动可扫码登录的专用 Google Chrome。
启动程序自动查找 Chrome，等待 CDP 就绪；9333 已有可用 Chrome 时通过 CDP 新建并激活可见窗口，不会只检测端口后退出。窗口控制使用项目依赖 `websockets`。
专用登录状态保存在 `data/chrome-profile-9333`，后续启动继续使用。

```powershell
.\启动Chrome9333.cmd --url "https://你的直播中控台地址"
python tools/start_chrome.py --port 9333
# 自定义安装位置：
python tools/start_chrome.py --chrome "D:\Chrome\Application\chrome.exe"
```

```powershell
.\tools\start-chrome.ps1
```

专用 Chrome 中人工扫码登录、打开抖音直播中控台，将**完整 URL**写入 `.env` 的 `LIVE_ROOM_URL`。
CDP 仅监听本机。不要使用日常浏览器 profile，也不要把 CDP 端口暴露到网络。

```powershell
.\.venv\Scripts\python -m live_agent.main doctor
```

选择器集中在 `config/platform.yaml`；评论选择器来自用户方案，需对实际页面复验。
当前商品、验证码和福袋选择器尚未实机标定，不提供猜测的可写选择器。
`product_root` 必须对应唯一商品卡，`product_id_attribute` 需提供稳定商品 ID。
如页面无此属性，应扩展适配器做标题/商品链接到商家 SKU 的显式映射。
`risk` 应定位可见的验证码或风险提示，配置为仅匹配活跃风险状态的选择器。

## 模式

- `shadow`：只采集、检索、生成候选；所有写操作被执行器拒绝。
- `assist`：候选需在面板批准；仍受 TTL、商品复验、限流约束。
- `auto`：有效知识支持的回复自动执行；公屏顶帖还需 `LIVE_ALLOW_PUBLIC=true`。
- 福袋需要 `auto` + `LIVE_ALLOW_BAGS=true` + 已标定适配器 + 到期计划。
- 启动模式：`python -m live_agent.main serve --mode assist`。

## 知识库与回答准确性

填写 `kb/*.yaml` 的 `entries`，格式参照 `kb/faq.yaml`。每条知识必须有商品 ID、关键词、审核答案、来源、带时区的有效期。
全局规则使用 `product_id: '*'`，避免把某商品事实写成全局规则。
检索支持商品过滤、关键词快速路径、中文 bigram FTS5，以及可选 Embedding 语义召回与 RRF 融合。
配置 `LIVE_EMBEDDING_MODEL`、`LIVE_EMBEDDING_API_KEY`、`LIVE_EMBEDDING_BASE_URL` 后执行 `python -m live_agent.main build-kb` 构建向量缓存。
Embedding 与聊天模型使用独立凭据；不会把 DeepSeek 密钥发往阿里云。向量维度通过 `LIVE_EMBEDDING_DIMENSIONS` 配置，默认 1024。
Qwen 接口地址使用百炼控制台给出的 OpenAI 兼容地址（需与工作空间和地域匹配）。
向量保存至 `data/knowledge.sqlite3`（`LIVE_VECTOR_DB_PATH` 可修改），使用 sqlite-vec 原生 `vec0` 余弦 KNN。
商品 ID 和知识有效期在 KNN 内过滤，当前商品与通用知识分别查询，再合并排序。
建库按条目哈希增量更新，仅新增/修改条目请求 Embedding；删除的知识同步移除，提交采用事务，失败保留旧快照。
模型、维度或接口地址变化会重建向量表；知识指纹不匹配时禁用旧向量召回，保留词法检索，并需重新建库。
旧 `data/vectors.json` 不再读取，也不会自动删除；执行 `build-kb` 从审核后的 YAML 源文件重建。
知识源在服务启动时加载；修改文件后先运行 `build-kb`，再重启服务。建库进程与服务使用独立连接及 WAL。
目前仍是精确 KNN，无 ANN 索引；大规模及并发性能需要另做压测。
语义/FTS 召回必须经过模型选择，不会直接自动套用弱匹配答案。Embedding 不可用时降级到词法召回。
与审核问题或 `aliases` 精确匹配、且只有唯一答案时直接返回；其他召回由模型选择，未配置模型则转人工。
模型输出必须与检索到的审核答案原文完全一致，并且附有有效依据 ID，否则拒绝发送。
这是保守的初版：不支持自由改写、多条知识综合回答；检索命中不等于事实已经得到商家核验，知识需人工维护。
知识文件在启动时加载，修改后重启服务。

模型配置：`LIVE_MODEL`、`LIVE_API_KEY`，以及可选的 `LIVE_BASE_URL`（OpenAI-compatible API）。
DeepSeek V4.1 Flash 配置 `LIVE_MODEL=deepseek-flash` 和 `LIVE_BASE_URL=https://api.deepseek.com`。
使用非思考模式及 function calling 结构化输出，结果仍由本地 Pydantic 和事实校验器检查。
弹幕视为不可信输入；工作流固定工具权限，不允许弹幕更改预算、限流或执行任意 JS。

## CDP 是 Agent 的工具与数据来源

`GET /api/tools` 提供工具 schema：

- `read_live_comments`：MutationObserver 增量评论，现存历史不重放。
- `read_current_product`：当前商品 ID 和字段。
- `read_lucky_bag`：活动状态。
- `search_knowledge`：商品隔离、有效期过滤后的审核知识。
- `send_reply`：只能发送已存储且通过校验的候选。
- `bump_comment`：差评可视范围检查、单任务最多五条、账号限流、小时配额。
- `start_lucky_bag`：只能执行预配置且到期的活动计划。

LangGraph 负责上下文 → 决策 → 校验；后台调度器通过同一工具执行动作。
执行器在模型之外校验模式、暂停、审批、TTL、商品和知识；所有页面写操作串行。
当前单进程、单账号、单房间；不支持同一数据库启动多个服务进程。

## 顶帖

`config/replies.yaml` 为人工维护的通用语库；生产建议扩充到至少 30 条。
多个待处理差评合并为一项顶帖任务，出屏停止，最多五条，默认每小时最多 20 条公屏常用语。
同句 30 分钟内不重复、同脱敏观众标识 2 分钟内不重复、账号每分钟最多四条。
脱敏昵称并非可靠用户 ID：当前会保守合并重名观众的冷却时间。
默认三条演示常用语不足以完成五次顶帖，额度不足会等待，180 秒后任务过期。
差评识别目前是关键词基线，复杂语义和误报仍需真实回放评测。

## 福袋

`config/bags.yaml` 配置一次性计划、带时区的触发时间、审批模板 ID、最大成本和字段。
只在计划时间之后 60 秒内执行，错过不补开。预算与次数上限为当前数据库累计值，保守地跨重启保留，不会自动清零。
活动 DOM 必须能可靠读到 `idle/pending/running` 状态、模板 ID 和唯一活动 ID。
创建前检查空闲，填写后逐字段读回；提交后核对模板和活动 ID，开启后再次核对状态。
提交前读取 `bag_cost` 的整数分成本属性，与审批预算严格比较；页面没有可靠成本字段时必须扩展适配器，不会猜测或跳过检查。
遇到已有待开始/进行中活动、超时或标识冲突就暂停，绝不盲目重试。

## 持久化与恢复

SQLite WAL 保存评论审计、候选、动作幂等键、发送额度和福袋预算。
发送前记录额度，超时视为可能已发送；结果不确定会暂停，需要人工核验。
重启后 `executing` 动作标记 `uncertain`，不自动重放。恢复按钮只解除暂停，不会清除幂等记录。
本地日志含观众评论，位于 `data/agent.sqlite3`；浏览器 profile 也在 `data/`，不要提交。
暂停阻止后续动作，不保证撤销已点击发送的动作。

## 验证

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests
cd web
npm run build
```

测试验证模式、审批、过期、知识隔离、商品切换、限流、幂等、预算和 API；不向真实直播间发送。
sqlite-vec 测试覆盖原生 KNN 商品/有效期过滤、重启持久化、增量更新/删除、维度变更和事务回滚。
`python tools/smoke_vector.py` 可用真实 Embedding API 验证入库检索，测试知识只写临时目录，不污染业务库。
`GET /api/status` 的 `knowledge` 字段显示后端、扩展版本、向量条数和索引与知识源是否一致。
Chrome 本地 DOM 测试使用独立 headless 实例，需已安装 Chrome，不连接日常浏览器。
离线真实弹幕评测：`python tools/replay.py path/to/evaluation.jsonl`；输入格式见脚本注释，输出逐条结果和准确率/P95。
候选 P95 在面板展示，但 500ms/3s 和准确率 95% 均是待实测目标，尚无真实场次验收结果。
尚待完成：实际页面标定、真实发送回读、福袋联调、真实弹幕评测、Markdown 切块和多场调度。

参考：[LangGraph StateGraph](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)、[Playwright CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)。
