# 冷链货物追责服务（Cold-Chain Accountability Service）

基于 **Python + FastAPI + PostgreSQL** 的冷链货物温度追责服务。
记录货物的测温事件与交接事件，按**发生时间**重建保管链，计算越温区间，
并在交接时刻把责任拆分给各责任方。

- 温度 **> 温度上限** 的测温开启越温区间；之后**第一笔不超温**（温度 ≤ 上限）的测温关闭区间。
- 区间跨越交接时，以**交接时刻**拆分责任：`[..., 交接时刻)` 归交出方，`[交接时刻, ...)` 归接收方。
- **晚到记录**（乱序补录）会在下次写入/查询时自动纳入，受影响区间自动重算。
- 同 `event_id` **同内容重传幂等**（不重复写入），**异内容拒绝**。
- 任何使**交出方不等于当时保管方**的写入**整体拒绝**，事务回滚，既有结果不变。
- **同一货物同一时刻**不得存在多笔测温或多笔交接（同时刻一笔测温 + 一笔交接允许）。
- 早于货物起始时间的事件拒绝。
- 未闭合的越温区间 `end` 返回 `null` 标记未结束。

---

## 1. 一键启动（Docker Compose，含数据库初始化）

需要本机已安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动内容：

| 服务 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| `api` | 本仓库构建（python:3.12-slim + uvicorn，2 worker） | **http://localhost:8000** | FastAPI 服务 |
| `db`  | postgres:16-alpine | localhost:5432（仅调试用，可关闭） | 首次启动自动执行 `db/init/01_init.sql` 建表 |

数据库初始化 SQL 同时放在两个位置（内容相同，双保险）：

- `db/init/01_init.sql` —— 挂载到 Postgres 的 `/docker-entrypoint-initdb.d`，**首次创建数据卷时自动执行**；
- `app/schema.sql` —— 应用启动时幂等执行（`CREATE TABLE/INDEX IF NOT EXISTS`）。

后台运行 / 停止 / 清空数据：

```bash
docker compose up -d --build      # 后台启动
docker compose logs -f api        # 查看日志
docker compose down               # 停止
docker compose down -v            # 停止并删除数据库数据卷（重新初始化）
```

### 健康检查与文档

- 健康检查：`GET http://localhost:8000/health`
- Swagger UI（可在线调用）：`http://localhost:8000/docs`
- ReDoc：`http://localhost:8000/redoc`
- 服务根路径列出全部 API：`GET http://localhost:8000/`

## 2. 配置

通过环境变量配置（可复制 `.env.example` 为 `.env`，`docker compose` 会自动读取）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_PORT` | `8000` | 发布到本机的 API 端口（容器内固定 8000） |
| `POSTGRES_USER` | `coldchain` | 数据库用户 |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码 |
| `POSTGRES_DB` | `coldchain` | 数据库名 |
| `POSTGRES_PORT_PUBLISH` | `5432` | 数据库发布到本机的端口（不需要可设为空） |
| `DATABASE_URL` | （空，由上面分项拼接为 `postgresql://...@db:5432/coldchain`） | 完整连接串，设置后优先使用 |

## 3. 本地非容器运行（可选，需自行提供 PostgreSQL 14+）

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://coldchain:coldchain@localhost:5432/coldchain
# 首次需在数据库执行 app/schema.sql（应用启动也会自动执行）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行核心算法的单元测试（无需数据库与第三方依赖）：

```bash
python3 -m unittest discover -s tests -v
```

## 4. API 一览

基址：`http://localhost:8000`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cargo` | 创建货物（编号、起始时间、温度上限、起始保管方） |
| GET  | `/api/cargo` | 货物列表 |
| GET  | `/api/cargo/{cargo_no}` | 货物详情 |
| POST | `/api/events/measurements` | 上报测温事件（事件ID、货物编号、发生时间、温度） |
| POST | `/api/events/handovers` | 上报交接事件（事件ID、货物编号、发生时间、交出方、接收方） |
| GET  | `/api/events?cargo_no=...` | 事件列表（按发生时间），可按货物过滤 |
| GET  | `/api/cargo/{cargo_no}/violations` | **越温追责报告**：越温起止、各责任方片段、保管链 |
| GET  | `/health` | 健康检查 |

### 请求约定

- 所有时间为 **带时区的 ISO 8601**：`2026-01-01T01:00:00Z` 或 `2026-01-01T09:00:00+08:00`；服务内部统一转 UTC。
- 温度为 JSON 字符串（如 `"6.20"`，最多 4 位小数），避免浮点误差；响应同样为字符串。
- 区间语义为左闭右开 `[start, end)`；`end` 为 `null` 表示未结束。

### 状态码与错误体

| 状态码 | 场景 | 错误 `code` |
|---|---|---|
| 201 | 货物/事件创建成功 | — |
| 200 | 同 ID 同内容重传（响应 `deduplicated: true`） | — |
| 404 | 货物不存在 | `cargo_not_found` |
| 409 | 交出方不是当时保管方；同时刻同类事件冲突；同 ID 异内容重传；货物编号/事件ID重复 | `invalid_custodian` / `same_time_conflict` / `event_id_content_mismatch` / `cargo_exists` |
| 422 | 事件早于货物起始时间；请求体/字段格式校验失败 | `event_before_start` |

错误响应体：`{"detail": "...", "code": "...", "event_id": "..."}`。

## 5. 调用示例

### 5.1 curl

完整端到端脚本见 [`examples/api_examples.sh`](examples/api_examples.sh)（含越温拆分、
晚到重算、幂等、各类拒绝、未闭合区间）：

```bash
./examples/api_examples.sh
```

手动调用：

```bash
# 创建货物：上限 5.0℃，起始保管方 WAREHOUSE-A
curl -s -X POST http://localhost:8000/api/cargo \
  -H 'Content-Type: application/json' \
  -d '{"cargo_no":"C-001","start_time":"2026-01-01T00:00:00Z",
       "temp_upper_limit":"5.0","initial_holder":"WAREHOUSE-A"}'

# 01:00 测温 6.0℃ —— 超过上限，开启越温区间
curl -s -X POST http://localhost:8000/api/events/measurements \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"m1","cargo_no":"C-001",
       "occurred_at":"2026-01-01T01:00:00Z","temperature":"6.0"}'

# 03:00 交接 A -> B（交出方等于当时保管方，接受）
curl -s -X POST http://localhost:8000/api/events/handovers \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"h1","cargo_no":"C-001",
       "occurred_at":"2026-01-01T03:00:00Z",
       "from_party":"WAREHOUSE-A","to_party":"CARRIER-B"}'

# 04:00 测温 4.5℃ —— 首次不超温，关闭区间
curl -s -X POST http://localhost:8000/api/events/measurements \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"m2","cargo_no":"C-001",
       "occurred_at":"2026-01-01T04:00:00Z","temperature":"4.5"}'

# 追责报告
curl -s http://localhost:8000/api/cargo/C-001/violations | python3 -m json.tool
```

报告（责任已在交接时刻 03:00 拆分）：

```json
{
  "cargo_no": "C-001",
  "temp_upper_limit": "5.0000",
  "start_time": "2026-01-01T00:00:00Z",
  "current_holder": "CARRIER-B",
  "custody_chain": [
    {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T03:00:00Z", "holder": "WAREHOUSE-A"},
    {"start": "2026-01-01T03:00:00Z", "end": null, "holder": "CARRIER-B"}
  ],
  "violations": [
    {
      "start": "2026-01-01T01:00:00Z",
      "end": "2026-01-01T04:00:00Z",
      "segments": [
        {"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T03:00:00Z", "party": "WAREHOUSE-A"},
        {"start": "2026-01-01T03:00:00Z", "end": "2026-01-01T04:00:00Z", "party": "CARRIER-B"}
      ]
    }
  ]
}
```

### 5.2 Python（标准库，无需安装第三方包）

```bash
python3 examples/call_api.py
```

### 5.3 晚到记录与重算

区间在 04:00 关闭后，若补录一笔 **02:30 的正常测温**（晚到）：

```bash
curl -s -X POST http://localhost:8000/api/events/measurements \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"m-late","cargo_no":"C-001",
       "occurred_at":"2026-01-01T02:30:00Z","temperature":"4.0"}'
```

再次查询报告：越温区间变为 `[01:00, 02:30)`，且因交接发生在 03:00，
**全部责任归 WAREHOUSE-A**；02:30 之后的高温读数会另开新的越温区间评估。
所有既有交接也会被重新校验：若晚到交接使某笔既有交接的交出方不再等于
当时保管方，该笔写入**整体拒绝**（409）。

## 6. 规则细节说明

1. **区间开合**：温度严格大于上限（`>`）开启；温度等于上限视为不超温，不开启新区间；
   已开启区间遇到第一笔温度 ≤ 上限的测温即关闭。
2. **交接时刻归属**：交接时刻本身归接收方。因此越温恰好在交接时刻关闭时，
   尾片段为零长度，接收方不承担该区间责任；越温恰在交接时刻开始时，责任从接收方起算。
3. **保管方校验是全量重放**：每次写入都会取出该货物全部事件按发生时间排序重放，
   从起始保管方开始逐笔交接。任何一笔交接的交出方与当时保管方不符（包括晚到数据
   导致的既有交接失效），整笔写入回滚。
4. **同时刻冲突**：数据库以部分唯一索引兜底（同一 `cargo_no + occurred_at`
   下 `measurement` / `handover` 各最多一笔），应用层也会预先校验。
5. **未闭合区间**：查询时仍处于越温状态（自开启后没有不超温测温），返回
   `"end": null`，其责任尾片段 `"end": null` 归当前保管方。
6. **并发**：写入时锁定货物行（`SELECT ... FOR UPDATE`），同一货物的并发写入串行化；
   事件 ID 全局唯一，同内容重传在任何情况下都不会产生重复数据。

## 7. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常映射
│   ├── schemas.py       # Pydantic 请求/响应模型
│   ├── services.py      # 事务、写入校验、报告查询
│   ├── domain.py        # 纯领域逻辑：保管链重建与责任拆分（可独立单测）
│   ├── db.py            # psycopg3 连接池
│   └── schema.sql       # 建表 DDL（启动幂等执行）
├── db/init/01_init.sql  # Postgres 首次初始化 SQL
├── tests/
│   ├── test_domain.py   # 核心规则单元测试（23 例，无需依赖）
│   └── test_http.py     # FastAPI 端到端 HTTP 测试（14 例，内存假库）
├── examples/            # curl 与 Python 调用示例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```
