# 在 1Panel 后以同域 `/api` 暴露监控 API

目标域名与路径：

```text
https://idadm.alexzsk.xyz/                  -> Streamlit（IDADM-Course-Helper，已有）
https://idadm.alexzsk.xyz/_stcore/stream    -> Streamlit WebSocket（已有）
https://idadm.alexzsk.xyz/api/v1/...        -> 本项目 FastAPI（新增）
```

HTTPS、证书和 80/443 端口继续由 1Panel 的 OpenResty 管理，**不要再安装 Caddy**，避免重复监听端口。

## 1. 先确认本项目容器正常

服务器上执行：

```bash
cd /opt/idadm-official-doc-monitor

docker compose ps
curl -s http://127.0.0.1:8000/healthz

# 必须是 127.0.0.1:8000，不能是 0.0.0.0:8000
ss -lntp | grep ':8000'
```

预期容器为 `running (healthy)`，健康检查返回 JSON，端口只绑定本机回环地址。

如果 `API_KEY` 还是空的，公网暴露前必须设置：

```bash
cd /opt/idadm-official-doc-monitor
if grep -q '^API_KEY=.\+' .env; then
  echo "API_KEY already set"
else
  KEY=$(openssl rand -hex 32)
  sed -i "s/^API_KEY=.*/API_KEY=$KEY/" .env
  chmod 600 .env
  docker compose up -d --force-recreate
  echo "new API key: $KEY"
fi
```

把生成的 key 保存到密码管理器。之后不要再打印它。

## 2. 在 1Panel 新建路径反代

进入：

```text
网站 -> idadm.alexzsk.xyz -> 配置 -> 反向代理 -> 创建反向代理
```

填写：

| 字段 | 值 |
| --- | --- |
| 名称 | `monitor-api` |
| 匹配规则 | `^~` |
| 前端请求路径 | `/api/` |
| 后端代理地址 | `http://127.0.0.1:8000` |
| 发送域名 | `$host`（若面板提供该字段） |

关键点：后端地址末尾**不要加 `/`**。否则 Nginx 会剥掉 `/api/` 前缀，把 `/api/v1/documents` 变成 `/v1/documents`，接口会 404。

保存后打开该规则的「源文」，确认生成的内容类似：

```nginx
location ^~ /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 10s;
    proxy_send_timeout 180s;
    proxy_read_timeout 180s;
}
```

不要写 `rewrite`，不要写 `proxy_pass http://127.0.0.1:8000/;`。本项目的路由本身已经带 `/api/v1`，原样转发即可。

已有的 Streamlit 根路径反代和 `/_stcore/stream` WebSocket 配置不要改动。`/api/` 是更长的前缀，会优先匹配。

### 如果面板没有「前端路径」功能

在站点的反向代理源文或高级配置中手动加入上面的 `location` 块。1Panel 常见路径：

```text
站点配置：/opt/1panel/www/conf.d/idadm.alexzsk.xyz.conf
反代规则：/opt/1panel/www/sites/idadm.alexzsk.xyz/proxy/
```

以实际面板版本为准。手工修改后检查语法并重载：

```bash
# 找到 OpenResty 容器名
docker ps --format '{{.Names}}' | grep -i openresty

docker exec <openresty-container> nginx -t
docker exec <openresty-container> nginx -s reload
```

## 3. 验证

### 3.1 不带 key，应当 401

```bash
curl -i https://idadm.alexzsk.xyz/api/v1/documents
```

预期 `HTTP/2 401`。如果返回 200，说明 `API_KEY` 没生效，先停止对外暴露。

### 3.2 带 key，应当 200

```bash
cd /opt/idadm-official-doc-monitor
KEY=$(sed -n 's/^API_KEY=//p' .env)

curl -s \
  -H "X-API-Key: $KEY" \
  https://idadm.alexzsk.xyz/api/v1/documents
```

### 3.3 Streamlit 不受影响

浏览器打开：

```text
https://idadm.alexzsk.xyz/
```

确认页面、交互和 WebSocket 均正常。

### 3.4 检查状态与调度

```bash
curl -s http://127.0.0.1:8000/healthz
docker compose exec monitor python -m app.cli status
```

健康信息中应看到 `check_interval_minutes: 10080`（若已应用周级配置）。

## 4. 对外调用地址

项目没有 `/api` 这个路由，`/api` 是反代前缀。实际调用基址是：

```text
https://idadm.alexzsk.xyz/api/v1
```

常用接口：

```bash
BASE=https://idadm.alexzsk.xyz/api/v1
KEY=<你的API_KEY>

# 所有文档状态
curl -s -H "X-API-Key: $KEY" "$BASE/documents"

# 单个文档
curl -s -H "X-API-Key: $KEY" "$BASE/documents/sea-course-list"

# 最近一次内容变更
curl -s -H "X-API-Key: $KEY" \
  "$BASE/documents/sea-course-list/changes/latest"

# 手动触发检查（可选，耗时较长）
curl -s -X POST -H "X-API-Key: $KEY" \
  "$BASE/documents/sea-course-list/check"
```

如需统一入口页，可以后续单独加一个 `/api` 路由；当前不要用 `/api` 作为具体请求地址。

## 5. 安全与运维

- 公网只开放 80/443 和 SSH，不开放 8000。
- `API_KEY` 必须设置；`/api/v1/check` 会触发对外抓取，不能匿名暴露。
- 本项目只反代 `/api/`，因此 `/docs`、`/openapi.json`、`/healthz` 不会出现在公网；需要时再加独立规则。
- 应用未开启 CORS。服务器到服务器的调用不受影响；浏览器跨域调用需要额外加 CORS。
- `proxy_read_timeout` 已设为 180 秒，避免手动触发检查时被反代提前断开。
- 仍保持单容器、单 worker；不要 `--scale`。
- 修改 `.env` 后使用 `docker compose up -d --force-recreate`。

更新服务：

```bash
cd /opt/idadm-official-doc-monitor
git pull --ff-only
docker compose up -d --build
```

备份：

```bash
cd /opt/idadm-official-doc-monitor
mkdir -p backups
docker compose stop monitor
docker compose cp monitor:/data ./backups/$(date +%F)
docker compose start monitor
```

## 6. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 404 | `proxy_pass` 末尾多了 `/`，`/api` 前缀被剥掉 | 去掉末尾斜杠 |
| 502 | OpenResty 无法访问 upstream | 检查 OpenResty 网络模式与 8000 端口绑定 |
| 401 | 未带 key 或 key 错误 | 这是预期行为，带 `X-API-Key` |
| 504 | 手动检查耗时超过反代超时 | 提高 `proxy_read_timeout`，或改为异步触发后轮询状态 |
| Streamlit 打不开 | 根路径反代或 WebSocket 配置被改动 | 恢复原 Streamlit 反代规则 |
| `/api/...` 返回 Streamlit 页面 | `location` 没有生效或路径不匹配 | 确认规则已启用，使用 `/api/` 前缀 |

### 502 时检查 OpenResty 网络

如果已有 Streamlit 反代的目标也是 `127.0.0.1:8501` 且工作正常，直接沿用 `127.0.0.1:8000` 即可。

如果 Streamlit 用的是容器名或 `172.x` 地址，说明 OpenResty 不在 host 网络。查看网络模式：

```bash
docker inspect <openresty-container> \
  --format '{{.HostConfig.NetworkMode}}'
```

不是 `host` 时，`127.0.0.1` 在 OpenResty 容器里指向它自己，需要改用宿主机在 Docker 网络中的网关地址，并让 monitor 监听该网关，或把两个容器接入同一 Docker 网络。不要为了省事把 8000 直接暴露到 `0.0.0.0`。

## 7. 公开更新页 `/updates`（可选）

监控服务自带一个免登录的更新页（status page 风格，回答「官方文档有没有更新文件」），本地路径为 `/updates`。要在公网访问 `https://idadm.alexzsk.xyz/updates`，按第 2 节同样的方式再加一条路径反代：

| 字段 | 值 |
| --- | --- |
| 前端请求路径 | `/updates` |
| 后端代理地址 | `http://127.0.0.1:8000` |
| 发送域名 | `$host` |

生成的 `location` 块等价于：

```nginx
location ^~ /updates {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 10s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
}
```

注意：

- 同样**不要**在 `proxy_pass` 末尾加 `/`。
- `location` 前缀写 `/updates`（**不带末尾斜杠**），否则裸路径 `/updates` 匹配不到这条规则、会落到 Streamlit 根路由。若 1Panel 面板自动补了 `/`，在源文里手改掉。
- `/updates` 与 `/updates/data.json` 不带 key 直接访问应返回 200——这是设计行为：该命名空间公开只读，只暴露非敏感字段，且不会触发任何检查。`/api/v1` 的 `API_KEY` 保护不受影响。
- 若暂时不想公开，不添加该 `location` 即可，页面只在服务器本机可见。
- 验证：`curl -s https://idadm.alexzsk.xyz/updates/data.json | head -c 200` 应返回 JSON。
