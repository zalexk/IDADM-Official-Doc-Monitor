# CUHK IDADM 官方文件变更监控与查询 API

> [!note]
>
> The project is powered by vibe-coding.

监控以下两份公开 PDF，并区分「文件字节被替换」和「实际内容发生变化」：

- `sea-course-list` — [IDA/SEA Course List, CUHK 2025-26](https://www.cdmp.cuhk.edu.hk/documents/IDA_SEA-Course%20list_CUHK_2025-26.pdf)
- `u-core-cuhk-cuhksz` — [University Core between CUHK and CUHK(SZ), cohorts 2024-25 to 2026-27](https://www.cdmp.cuhk.edu.hk/documents/U%20core%20between%20CUHK%20&%20CUHKSZ_cohort%202024_25.pdf)

变更结果写入 SQLite，并通过 FastAPI 暴露状态、变更列表、变更详情、Markdown 报告和纯文本 unified diff。项目目录独立，不依赖同级其他项目。

后续开发约束、测试规则和完成标准见 [AGENTS.md](AGENTS.md)。

## 1. 可行性评估

### 1.1 目标文件实测

这两份 PDF 都是**文本型 PDF**，不是扫描图片；文字和表格都可以用 PyMuPDF 直接抽取。原站是 Apache 静态文件服务，支持条件请求：

| 文件 | 大小 | 页数 | `Last-Modified` | `ETag` |
| --- | ---: | ---: | --- | --- |
| Course list | 168,651 B | 11 | `Mon, 14 Sep 2026 07:10:14 GMT` | `"292cb-65b6c2022a180"` |
| U Core | 195,646 B | 3 | `Mon, 27 Jul 2026 01:59:00 GMT` | `"2fc3e-6578e10daa100"` |

实测第二次请求返回 `304 Not Modified`，因此日常轮询可以做到很轻量。

### 1.2 能做到什么

| 问题 | 能否回答 | 实现方式 |
| --- | --- | --- |
| 文件有没有被替换？ | 能 | 条件 GET + 响应体 SHA-256 |
| 替换后内容有没有变？ | 能 | 归一化文本 + 表格单元格的内容指纹 |
| 哪些页变了？ | 能 | 页面内容对齐 + 页级文本 diff |
| 哪个表格、哪一行、哪一列变了？ | 能，规则表格效果最好 | 表格对齐 + 行对齐 + 单元格 diff |
| 增加了/删除了哪些文字？ | 能 | 页级 unified diff |
| 只是重新导出 PDF、内容没变？ | 能识别为 `metadata_only` | 内容指纹不变，但字节哈希变化 |
| 具体“为什么改”“是否等价” | 不能自动判断 | 输出的是字面差异，不是语义结论 |
| 扫描件/文字转曲线后的内容差异 | 不能定位 | 仍能发现文件变化；需要 OCR/视觉 diff 扩展 |

本次已经用真实 Course list PDF 做过一次“只改一个课程代码”的实验：把 `MATH3270` 改成 `MATH3271` 后，系统准确报告：

> 第 3 页，表格 1，第 1 行，第 1 列：`MATH3270` → `MATH3271`

### 1.3 关键限制

- 这是**字面差异**工具，不做语义理解。例如表格列宽变化导致换行位置变化，可能产生额外的文本行差异；表格级 diff 会尽量抵消这种噪声。
- 如果将来 PDF 变成扫描件，或者文字被转成曲线，文本层会消失，系统会降级为「只能发现字节变化」，变更详情会明确标记 `text_extractable=false`。
- 表格对齐是启发式的。如果表格被大幅重排、合并单元格或跨页拆表，单元格级结果可能不如文本 diff 稳定；Markdown 和 unified diff 仍然可用。
- 原站如果更换 URL，当前配置会得到 404 并记录错误，需要更新 `config/documents.yaml`。
- 不内置邮件/Telegram/Webhook 推送；按需求提供了查询 API，外部服务可自行轮询。需要推送时可在 `insert_change` 后增加 webhook。

### 1.4 结论

技术上可行，而且这两份文件属于比较理想的监控对象：文本层完整、表格边界清晰、原站支持 ETag。建议采用“HTTP 条件请求 + SHA-256 + 内容指纹 + 页/表格 diff”的组合，而不是只比较文件哈希或只抓网页文本。

## 2. 架构与检测流程

```text
                    ┌──────────────────────────────┐
                    │  CDMP 静态 PDF (2 个 URL)     │
                    └──────────────┬───────────────┘
                                   │ HTTP GET（默认每周一次完整下载）
                                   │ 可配置 If-None-Match / If-Modified-Since
                                   ▼
                    ┌──────────────────────────────┐
                    │ fetcher.py                   │
                    │ 304 / 429 / 5xx / 超时重试   │
                    └──────────────┬───────────────┘
                                   │ 200 + bytes
                                   ▼
              ┌────────────────────────────────────────┐
              │ SHA-256 与上一版本相同？                │
              │   是 → unchanged，只更新检查时间        │
              └───────────────────┬────────────────────┘
                                  │ 不同
                                  ▼
              ┌────────────────────────────────────────┐
              │ PyMuPDF 抽取文本 + 表格                 │
              │ 归一化文本/单元格 → 内容指纹            │
              └───────────────────┬────────────────────┘
                                  │
              ┌───────────────────┴────────────────────┐
              │ 内容指纹与上一内容版本相同？            │
              │   是 → metadata_only（不生成变更报告）  │
              └───────────────────┬────────────────────┘
                                  │ 不同
                                  ▼
              ┌────────────────────────────────────────┐
              │ differ.py                               │
              │ 页对齐 → 表格对齐 → 行对齐 → 单元格 diff │
              │ 页级文本 unified diff                   │
              └───────────────────┬────────────────────┘
                                  ▼
              ┌────────────────────────────────────────┐
              │ SQLite + 磁盘快照                       │
              │ versions / changes / checks             │
              └───────────────────┬────────────────────┘
                                  ▼
              ┌────────────────────────────────────────┐
              │ FastAPI                                 │
              │ status / changes / diff / markdown      │
              └────────────────────────────────────────┘
```

### 为什么需要四层而不是一层

1. **HTTP 层**：`ETag` / `Last-Modified` 让 99% 的检查只收到 304，节省带宽，也减少对原站的压力。
2. **字节层**：SHA-256 是“文件确实被替换”的最终依据。即使服务器返回 200 且内容相同，也不会误报内容变化。
3. **内容层**：PDF 重新导出、时间戳变化、字体子集变化都会改变字节，但文本和表格可能完全相同。内容指纹把这些情况归为 `metadata_only`，避免“每次检查都说变了”。
4. **差异层**：页面、表格、行、单元格对齐后输出字面差异。页面和表格都使用序列对齐，插入一页不会导致后面所有页都被误报为修改。

### 内容指纹的定义

- 抽取每一页的文本：Unicode NFKC、统一换行、压缩连续空格/空行、去除零宽字符。
- 抽取每一页的表格：行列单元格值，单元格内换行合并为空格，去掉全空行和尾部全空列。
- **内容哈希不包含** PDF 元数据、文件时间戳、表格坐标/列宽等纯排版信息。
- 页码作为页面文本的一部分参与哈希，因此页面内容发生移动通常也会触发变化；这是有意的保守策略。

### 防漏检设计

- 默认每 7 天检查一次，且每次都是**不带条件头**的完整下载。周级频率下两份小 PDF 的流量可以忽略，也避免原站 ETag 配置错误或中间缓存导致漏检。
- 如果以后提高检查频率，可把 `FULL_CHECK_INTERVAL_HOURS` 设为 `24`，让多数检查走 304，每天至少完整下载一次。
- 网络错误和 429/5xx 最多重试 3 次，指数退避；4xx 不重试。
- 失败不会覆盖上一个成功版本，只记录 `last_error` 和 `consecutive_failures`。
- 每次检查都写入 `checks` 审计表（每文档保留最近 500 条）。

## 3. 技术栈

| 组件 | 选择 | 原因 |
| --- | --- | --- |
| 语言 | Python 3.13 | PDF/HTTP 生态成熟，部署简单 |
| Web API | FastAPI + Uvicorn | 类型化 JSON、自动 OpenAPI、异步 |
| HTTP | httpx | 流式下载、超时、条件请求 |
| PDF | PyMuPDF 1.28.2 | 文本和规则表格抽取质量好，依赖单一 |
| 调度 | APScheduler 3.x | 单进程内每 7 天触发，无需额外 Redis/Celery |
| 存储 | SQLite (WAL) | 只有两份文档，单机零运维；状态和审计足够 |
| 部署 | Docker Compose | 固定依赖和运行环境 |
| 反向代理 | Caddy / Nginx（建议） | HTTPS、域名、限流、访问控制 |

没有使用 Selenium/Playwright：PDF 是静态文件，不需要浏览器渲染，浏览器方案更重且更容易被页面结构变化影响。也没有引入 LLM：当前需求是“发现并定位字面变化”，规则 diff 更可验证、成本更低。若以后需要“把变化翻译成业务语义摘要”，可以再加 LLM 层。

### 规模扩大时

当前架构面向单机、低频、少量文档。若要监控几十上百份文件或做多副本 API：

- SQLite 换 PostgreSQL；
- 调度器从 API 进程拆成独立 worker；
- 快照放 S3/对象存储；
- 增加反向代理限流、Prometheus 指标和告警；
- 把 `metadata_only` 与 `content_changed` 事件接到消息队列。

## 4. 项目结构

```text
IDADM-Official-Doc-Monitor/
├── app/
│   ├── api.py           # FastAPI 工厂、鉴权、路由
│   ├── cli.py           # init-db / check / status / changes / show-change
│   ├── config.py        # 环境变量和 documents.yaml
│   ├── db.py            # SQLite schema 与查询
│   ├── differ.py        # 页/表格/行/单元格/文本 diff
│   ├── extractor.py     # PyMuPDF 抽取与内容指纹
│   ├── fetcher.py       # 条件 GET、重试、大小/类型校验
│   ├── main.py          # uvicorn app.main:app
│   ├── monitor.py       # 核心检查流程、快照、版本和变更落库
│   ├── presenters.py    # DB row → API 响应
│   ├── scheduler.py     # APScheduler
│   ├── schemas.py       # Pydantic 响应模型
│   └── timeutil.py      # UTC 时间工具
├── config/documents.yaml
├── tests/
├── AGENTS.md            # 后续 vibe-coding 与维护约束
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

运行数据默认在 `data/`：

```text
data/
├── monitor.db
└── snapshots/
    └── <document-key>/
        ├── <sha256>.pdf     # 原始 PDF 快照
        └── <sha256>.json    # 抽取后的页文本和表格
```

## 5. 本机启动

要求 Python 3.13+。Windows PowerShell / Git Bash 均可。

```bash
cd IDADM-Official-Doc-Monitor

# 创建虚拟环境（Windows 示例）
py -3.13 -m venv .venv
source .venv/Scripts/activate

pip install -r requirements.txt

# 初始化数据库和文档登记
python -m app.cli init-db

# 第一次检查：建立基线，不会报“内容变更”
python -m app.cli check

# 查看状态
python -m app.cli status
```

启动 API：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

- OpenAPI 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/healthz>
- API 默认无鉴权；生产环境务必设置 `API_KEY`。

默认 `SCHEDULER_ENABLED=true`，启动 API 后会在约 2 秒后自动做一次检查，之后每 7 天一次。服务重启也会立即再检查一次。只想手动运行 CLI 时，设置 `SCHEDULER_ENABLED=false`。

### 手动检查

```bash
python -m app.cli check --document sea-course-list
python -m app.cli check --full        # 忽略 ETag/Last-Modified，强制完整下载
python -m app.cli changes --document sea-course-list
python -m app.cli show-change --document sea-course-list --change-id 1 --format markdown
```

## 6. Docker 部署（推荐 Linux 服务器）

```bash
cp .env.example .env

# 生成 API Key，写入 .env
python -c "import secrets; print(secrets.token_hex(32))"

# 编辑 .env：
#   API_KEY=...
#   USER_AGENT=...（把联系邮箱改成你的）
#   CHECK_INTERVAL_MINUTES=10080   # 7 天
#   FULL_CHECK_INTERVAL_HOURS=0   # 每次检查都完整下载

docker compose up -d --build
docker compose logs -f
curl http://127.0.0.1:8000/healthz
```

Compose 默认只把端口绑定到 `127.0.0.1:8000`，外部服务通过反向代理访问。Caddy 示例：

```caddyfile
monitor.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

如果确实要直接暴露端口，把 `docker-compose.yml` 中的端口改成 `"8000:8000"`，并同时设置强 `API_KEY`、防火墙和 HTTPS；不建议裸奔。

Docker 数据在命名卷 `monitor_data` 中，重建镜像不会丢失。备份时先停服务再复制：

```bash
docker compose stop monitor
docker compose cp monitor:/data ./backup-data-$(date +%F)
docker compose start monitor
```

恢复时把备份内容复制回 `monitor:/data`，注意保持 SQLite 和 `snapshots/` 一致。

### systemd 裸机部署（可选）

```ini
[Unit]
Description=IDADM Official Document Monitor
After=network-online.target

[Service]
Type=simple
User=monitor
WorkingDirectory=/opt/idadm-official-doc-monitor
EnvironmentFile=/opt/idadm-official-doc-monitor/.env
ExecStart=/opt/idadm-official-doc-monitor/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`EnvironmentFile` 中的带空格值建议用双引号包起来（`.env.example` 的 `USER_AGENT` 已加引号）。

如果不想让 API 进程内置调度器，可以设置 `SCHEDULER_ENABLED=false`，用 cron 运行 CLI：

```cron
*/30 * * * * cd /opt/idadm-official-doc-monitor && SCHEDULER_ENABLED=false .venv/bin/python -m app.cli check >> /var/log/idadm-official-doc-monitor.log 2>&1
```

不要同时启用内置调度器和 cron，否则两个进程可能重复抓取；当前设计按单机单写者使用。

## 7. API 使用

### 鉴权

设置了 `API_KEY` 后，所有 `/api/v1/*` 请求必须带以下任一：

```http
X-API-Key: <key>
```

或：

```http
Authorization: Bearer <key>
```

`/healthz` 和 `/docs` 不需要鉴权。未设置 `API_KEY` 时所有接口公开，仅适合本机或可信内网。

部署时的选择：

- **通过公网域名暴露**：必须设置 `API_KEY`，否则任何人都能调用 `POST /check` 触发对外抓取，也能读取完整变更历史。
- **仅本机或可信内网访问**：可以留空；但要确认端口没有映射到公网。
- **调用方不方便加请求头**：优先改为内网访问或用反向代理限制来源 IP，不建议为了省事直接取消鉴权。
- 修改 `API_KEY` 后需要重建容器：`docker compose up -d --force-recreate`。

### 文档 key

| key | 用途 |
| --- | --- |
| `sea-course-list` | IDA/SEA 课程对照表 |
| `u-core-cuhk-cuhksz` | CUHK / CUHK(SZ) University Core 安排 |

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 数据库、调度器、下次运行时间 |
| GET | `/api/v1/documents` | 所有文档的状态摘要 |
| GET | `/api/v1/documents/{key}` | 单文档完整状态 |
| GET | `/api/v1/documents/{key}/versions` | 版本历史（分页） |
| GET | `/api/v1/documents/{key}/changes` | 变更列表（分页，倒序） |
| GET | `/api/v1/documents/{key}/changes/latest` | 最近一次内容变更详情 |
| GET | `/api/v1/documents/{key}/changes/{id}` | 指定变更详情（JSON） |
| GET | `/api/v1/documents/{key}/changes/{id}/markdown` | 变更 Markdown 报告 |
| GET | `/api/v1/documents/{key}/changes/{id}/diff` | 纯文本 unified diff |
| POST | `/api/v1/documents/{key}/check` | 立即检查单份文档 |
| POST | `/api/v1/check` | 立即检查全部文档 |

### 外部服务如何判断“有没有更改”

最简单、最稳定的做法是保存一个游标：`content.content_hash` 或 `latest_change.id`。

- `content.content_hash`：**内容**指纹。只有文本/表格内容变化才变；PDF 元数据重写不会变。
- `latest_change.id`：最近一次内容变更的递增 ID。适合“只处理新变更”的消费者。
- `file_changed_at`：文件字节最后一次变化时间（包括 `metadata_only`）。
- `content_changed_at`：内容最后一次真实变化时间。
- `latest_change` 为 `null` 表示自从建立基线以来没有内容变化。

状态示例（截取）：

```json
{
  "key": "sea-course-list",
  "source": {
    "etag": "\"292cb-65b6c2022a180\"",
    "last_modified": "Mon, 14 Sep 2026 07:10:14 GMT",
    "last_full_check_at": "2026-10-18T16:15:28Z"
  },
  "monitor": {
    "last_checked_at": "2026-10-18T16:15:38Z",
    "last_success_at": "2026-10-18T16:15:38Z",
    "last_http_status": 304,
    "consecutive_failures": 0,
    "last_error": null
  },
  "content": {
    "id": 1,
    "sha256": "a99f46a25ffb7cc9...",
    "content_hash": "sha256:4dfe7fb31528417a...",
    "page_count": 11,
    "text_extractable": true
  },
  "content_changed_at": null,
  "latest_change": null,
  "last_check": {
    "outcome": "not_modified",
    "http_status": 304
  }
}
```

变更详情示例（截取）：

```json
{
  "id": 1,
  "headline": "1 页内容变化，1 个表格单元格变化",
  "summary": {
    "pages_changed": 1,
    "table_cells_modified": 1
  },
  "diff": {
    "pages": [
      {
        "status": "modified",
        "new_page": 3,
        "table_changes": [
          {
            "type": "cell_modified",
            "table_index": 1,
            "old_row": 1,
            "new_row": 1,
            "column": 1,
            "old": "MATH3270",
            "new": "MATH3271"
          }
        ]
      }
    ]
  }
}
```

### 轮询示例

```python
import json
import pathlib

import requests

BASE = "https://monitor.example.com"
HEADERS = {"X-API-Key": "YOUR_KEY"}
STATE = pathlib.Path("monitor-cursor.json")

state = json.loads(STATE.read_text()) if STATE.exists() else {}
cursor = state.get("sea-course-list", {}).get("latest_change_id")

status = requests.get(
    f"{BASE}/api/v1/documents/sea-course-list", headers=HEADERS, timeout=20
).json()

latest = status.get("latest_change")
if latest and latest["id"] != cursor:
    detail = requests.get(
        f"{BASE}/api/v1/documents/sea-course-list/changes/{latest['id']}",
        headers=HEADERS,
        timeout=20,
    ).json()
    print(detail["markdown"])
    state.setdefault("sea-course-list", {})["latest_change_id"] = latest["id"]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
```

注意首次接入时 `latest_change` 可能为 `null`，也可能已经有历史变更。接入时应把当时看到的 `latest_change.id` 当作初始游标，避免把历史变更误当成新事件。

### 检查结果语义

`POST /check` 返回 200 只表示“检查流程执行完成”，不等于原站一定成功。请读取 `outcome`：

| outcome | 含义 |
| --- | --- |
| `initialized` | 首次建立基线 |
| `not_modified` | 原站返回 304 |
| `unchanged` | 完整下载后字节未变 |
| `metadata_only` | 字节变了，但抽取出的文本/表格没变 |
| `content_changed` | 内容确实变了，`change_id` 可用 |
| `error` | 网络/HTTP/解析失败，查看 `message` 和文档状态里的 `last_error` |

## 8. 配置

环境变量（也见 `.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `API_KEY` | 空 | 空表示不鉴权；生产必须设置 |
| `CHECK_INTERVAL_MINUTES` | `10080` | 轮询间隔（分钟），默认 7 天 |
| `SCHEDULER_ENABLED` | `true` | 是否在 API 进程内定时检查 |
| `CHECK_ON_START` | `true` | 启动后是否立即检查一次 |
| `FULL_CHECK_INTERVAL_HOURS` | `0` | 至少多久做一次完整下载；`0` 表示每次都完整下载，周级检查默认使用此值 |
| `HTTP_TIMEOUT_SECONDS` | `60` | 单次 HTTP 超时 |
| `MAX_DOWNLOAD_BYTES` | `26214400` | 下载大小上限，防止异常响应 |
| `USER_AGENT` | 内置值 | 建议改成带真实联系方式的标识 |
| `DATA_DIR` | 本地 `./data`；Docker `/data` | 数据库和快照目录 |
| `DOCUMENTS_CONFIG` | `config/documents.yaml` | 文档清单 |
| `DATABASE_PATH` | `$DATA_DIR/monitor.db` | 可单独覆盖 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `MAX_DIFF_CHARS` | `250000` | unified diff / Markdown 输出上限 |

新增文档：编辑 `config/documents.yaml`，增加唯一的 `key`、标题、直接 PDF URL 和标签，重启服务即可。新增文档第一次检查只建立基线，不会产生变更事件。

## 9. 运维要点

- **单 worker**：内置调度器依赖单进程。Dockerfile 已固定 `--workers 1`；裸机启动也不要开多 worker。需要多副本时，禁用内置调度器，改用单一外部 worker/cron。
- **升级 PDF 库**：`requirements.txt` 固定了 PyMuPDF 版本。升级 PyMuPDF 可能改变文本/表格抽取结果，导致内容指纹变化，建议升级后做一次完整检查并观察是否出现一次性误报。
- **磁盘**：每次不同的 PDF 字节都会保存一份快照。当前两份文件很小；如果以后文件很大或变化频繁，需要加保留策略（目前保留全部版本）。
- **日志**：Docker 使用 `json-file` 并限制 10 MB × 3。业务日志包含每次检查的 outcome、HTTP 状态、耗时和变更 headline。
- **安全**：设置强 `API_KEY`；优先用 Caddy/Nginx 提供 HTTPS；Compose 默认只监听 `127.0.0.1`；如果需要浏览器跨域访问，当前未开启 CORS，需要在代理层或应用层自行添加。
- **限流**：应用未内置限流。公开部署时请在反向代理层限流。
- **时间**：API 所有时间戳均为 UTC，带 `Z` 后缀。
- **礼貌抓取**：默认每周一次完整下载。周级频率对原站压力很小；如果以后调高频率，建议把 `FULL_CHECK_INTERVAL_HOURS` 设为 `24`，让多数检查走条件请求。请把 `USER_AGENT` 中的联系方式改成你自己的，并遵守原站使用条款；不要缩短到分钟级造成压力。

## 10. 验证记录

本次实现已实际验证：

- 两个真实 URL 均返回 `200 application/pdf`，带 ETag / Last-Modified；
- 第二次检查均收到 `304 Not Modified`；
- Course list 抽取 11 页，U Core 抽取 3 页，`text_extractable=true`；
- 在真实 Course list 副本上把 `MATH3270` 改为 `MATH3271`，差异定位到第 3 页、表格 1、第 1 行第 1 列；
- 10 个自动化测试通过，覆盖：单元格 diff、metadata-only 不误报、新增页对齐、多页同时修改对齐、API 鉴权、完整检查、基线、304、版本回退；
- Uvicorn + APScheduler 实际启动验证：启动后自动抓取，健康检查显示下次运行时间，带/不带 API Key 的 401/200 行为正确。

本机编写环境没有启动 Docker daemon，因此 Docker 镜像构建未实际执行；Dockerfile 和 compose 按标准方式提供，部署时请先运行 `docker compose build` 验证目标机器的基础镜像和架构兼容性。

测试命令：

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 11. 后续可扩展

- **变更推送**：在 `app/monitor.py` 生成 `change_id` 后调用 webhook/邮件/Telegram，不必等外部轮询。
- **视觉差异 / OCR**：对扫描 PDF 渲染页面并做像素 diff，或接 OCR；作为文本层缺失时的降级方案。
- **语义摘要**：把结构化 diff 交给 LLM，总结成“哪门课从 A 改成了 B”，同时保留原始字面 diff 作为证据。
- **多文档/HA**：PostgreSQL + 独立 worker + 对象存储 + 外部调度，API 可水平扩容。
- **可视化页面**：展示时间线、页面对比和单元格高亮；当前先满足 API 消费。
- **Prometheus/告警**：暴露检查成功率、失败次数、下次运行时间等指标。

## 12. 使用边界

本项目只读取公开 PDF 并低频轮询，不绕过登录、不修改原站内容。变更检测结果是字面比较，不构成官方课程安排解释；实际选课/学分认定请以 CUHK 官方通知和教务处答复为准。
