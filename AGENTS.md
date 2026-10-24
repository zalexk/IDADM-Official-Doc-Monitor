# AGENTS.md

本文件是这个项目的协作说明，供后续 vibe-coding、代码代理和人类维护者使用。README 是使用与部署说明，本文件是开发约束与工作方式。两者冲突时，先以实测结果为准，并同步修正文档。

## 项目定位

监控 CUHK CDMP 发布的 9 份公开 PDF（IDAADM 官网 curriculum.html 上的全部文件），判断：

1. 文件字节是否变化；
2. 内容是否真正变化；
3. 如果变化，具体改了哪一页、哪张表、哪一行、哪个单元格。

结果落地到 SQLite 和不可变快照，通过 FastAPI 提供给外部服务查询。

这不是通用爬虫，也不是通用 PDF diff 工具。需求边界是「少量固定 URL、低频轮询、内容级变更追踪、单机部署」。

## 必须保守的不变量

### 单写者与单 worker

- APScheduler 运行在 API 进程内，**只能有一个 API 进程或容器实例**。
- 禁止 `uvicorn --workers > 1`、禁止 `docker compose up --scale monitor=2`。
- 禁止同时启用内置调度器和 cron；两者都会写数据库、抓取同一批 URL。
- 如果将来需要多副本，必须先把调度器拆成独立 worker，并加分布式锁。不要只改部署参数。

### 变更语义不能含糊

检查结果只有这几种：`initialized`、`not_modified`、`unchanged`、`metadata_only`、`content_changed`、`error`。

- 第一次成功抓取只建立基线，**绝不能报告为变更**。
- 字节变化但文本和表格指纹相同，只能记 `metadata_only`；不要生成虚假的 `content_changed`。
- 抓取失败不能覆盖上一个成功版本，也不能推进 `content_hash` 或 `latest_change`。
- `latest_change: null` 表示建立基线以来没有内容变化，不是数据缺失。
- PDF 元数据、字体子集、对象顺序、表格坐标/列宽不应进入内容指纹。修改指纹算法前必须评估：算法一变，下一次检查可能把所有文档误判为内容变化。必须提供迁移或重新建基线方案，不能静默改。

### 快照与历史不可变

- `data/snapshots/<document-key>/<sha256>.pdf|json` 是审计依据，只增不改、不删除、不覆盖。
- `versions` 和 `changes` 一经写入不可回写；修正逻辑应新增记录或做显式迁移。
- 同一 PDF 内容回退到旧字节时，也应生成新的版本记录；不要恢复旧的唯一约束。
- 删除文档或清理磁盘前必须确认 API、报告和备份都不再引用对应快照。

### 存储与时间

- SQLite 使用 WAL，只放本机磁盘；禁止 NFS/SMB 等网络文件系统。
- 数据库与 `snapshots/` 必须保持一致，备份时先停服务再复制。
- 所有 API 时间戳使用 UTC、ISO 8601、带 `Z`；只在客户端做时区转换。
- 数据目录、数据库路径、文档配置都由环境变量控制，不要在代码里写死部署路径。

### 项目边界与秘密

- 不要读取、修改、格式化同级目录 `IDADM/` 或 `IDADM-Course-Helper/`，除非用户明确要求。它们是独立项目。
- 不要提交 `.env`、`data/`、`.venv/`、日志或任何密钥。
- 不要在日志、异常、测试快照或 API 示例中打印真实 `API_KEY`。
- 公网部署必须设置 `API_KEY`；仅本机或可信内网可以留空。

## 代码地图

| 文件 | 职责 |
| --- | --- |
| `app/config.py` | 环境变量、`Settings`、`documents.yaml` 校验 |
| `app/fetcher.py` | 条件 GET、重试、大小限制、PDF 校验 |
| `app/extractor.py` | PyMuPDF 文本/表格抽取、归一化、内容指纹 |
| `app/differ.py` | 页/表/行/单元格对齐、unified diff、Markdown 报告 |
| `app/monitor.py` | 单次检查流程、版本与变更写入、失败处理 |
| `app/db.py` | SQLite schema、连接管理、查询 |
| `app/scheduler.py` | APScheduler 封装、启动即检查、下一次运行时间 |
| `app/presenters.py` | 数据库行到稳定 API JSON 的转换 |
| `app/schemas.py` | Pydantic 响应模型 |
| `app/api.py` | FastAPI 工厂、鉴权、路由、lifespan |
| `app/updates_page.py` | `/updates` 更新页路由：公开只读、聚合 JSON、页面私有契约 |
| `app/static/updates.html` | 更新页本体：自包含 HTML/CSS/JS，无 CDN、无构建步骤 |
| `app/cli.py` | `init-db`、`check`、`status`、`changes`、`show-change` |
| `config/documents.yaml` | 被监控 URL 的登记表（9 份）；约定每个文档的**第一个 tag 是 `/updates` 页面的分组类别**（`equivalence-cuhk`/`equivalence-sz`/`u-core`/`other`），`tests/test_documents_config.py` 强制校验 |
| `tests/` | 合成 PDF、mock HTTP origin、差分与 API 测试 |

## 常用命令

Windows Git Bash：

```bash
cd C:/Users/alex/Documents/IDADM/IDADM-Official-Doc-Monitor
source .venv/Scripts/activate

python -m app.cli init-db
python -m app.cli check
python -m app.cli check --document sea-course-list
python -m app.cli check --full
python -m app.cli status
python -m app.cli changes --limit 10
python -m app.cli show-change --document sea-course-list --change-id 1 --format markdown

uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
pytest -q
```

安装依赖：

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

部署：

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

## 开发约定

- Python 3.13+；优先标准库，新增运行时依赖必须有明确必要性，并固定精确版本。
- 公开函数加类型标注；保持现有 dataclass / Pydantic 风格。
- 配置只从环境变量进入 `Settings`；新增配置时同步更新 `.env.example` 和 README。
- API 统一挂在 `/api/v1` 下；对外字段由 `presenters.py` 输出，不要让数据库列名直接泄漏到 API。
- API 变更以向后兼容为默认：新增字段可以，重命名或删除字段必须升级 API 版本。
- `/updates` 是给人看的更新页命名空间：公开只读、不走 API Key、只处理 GET、绝不触发 `POST /check`、不进 OpenAPI。它只暴露非敏感字段（公开 PDF 的标题/URL/时间戳/变更报告），与 `app/static/updates.html` 一起版本化，不受 `/api/v1` 的向后兼容承诺约束；改动 `updates.html` 的脚本时跑 `tests/test_updates_page.py::test_updates_page_script_has_valid_syntax`。页面文案支持简/繁/英三语（`I18N` 字典内联在 `updates.html`），默认跟随浏览器语言，用户选择存 `localStorage`；数据库里的变更报告（headline/markdown）仍始终是中文。
- 用户可见的变更报告用中文；代码、变量名、docstring 用英文。日期与文件路径使用跨平台写法。
- 网络请求只在 `fetcher.py` 中做；不要在路由或差分代码里直接发 HTTP。
- 抓取保持低频、带真实联系方式的 `User-Agent`，不要压测原站。
- 错误处理保留上下文，但不要吞异常；失败只记录状态，不破坏上一次成功版本。
- 不引入后台任务框架、消息队列或 PostgreSQL，除非需求明确要求横向扩展。

## 测试规则

- 测试必须离线：使用 `tests/pdf_factory.py` 生成合成 PDF，使用 `tests/conftest.py` 的 mock origin 模拟 ETag/304。
- 不允许在自动化测试中请求真实 CDMP URL。
- 修 bug 先写复现用例；涉及内容指纹的改动必须覆盖「元数据变化但内容不变」。
- 涉及 diff 的改动至少覆盖：单元格修改、页面新增、多页同时修改、页面移动或重排。
- 涉及 API 的改动至少覆盖：未鉴权 401、正确 API Key 200、未知文档 404、首次基线不产生变更、304 不产生新版本。
- 提交前跑完整 `pytest -q`，全部通过再报告完成。测试失败时不要用「应该没问题」代替验证。

## 完成标准

一次改动只有在以下条件都满足时才算完成：

1. 行为符合 README 对 API、检查结果和变更语义的承诺；
2. 新逻辑有对应测试，且全套测试通过；
3. 新增配置、接口、部署步骤已写入 README / `.env.example`；
4. 没有引入多 worker、重复调度、快照覆盖或密钥泄漏；
5. 明确区分「已验证」和「未验证」。没有实际执行的 Docker 构建、真实网络请求或部署，不得声称成功。

## 已知限制

- 只支持带文本层的 PDF；扫描件没有文本时内容退化为字节哈希，无法给单元格级 diff。
- 表格抽取是启发式的，复杂合并单元格可能定位不完美；报告用于发现和定位，不应当作法律意义上的逐字校对。
- 轮询只能看到两次检查之间的最终状态，无法发现「改了又改回去」的瞬时变化。
- 原站更换文件名或 URL 时，需要更新 `config/documents.yaml`；旧 URL 的 404 会作为错误记录，不会自动迁移。
- PDF 元数据被有意排除，因此「文件更新但内容没变」不会触发内容变更报告。
