# 电表日结 SQL 核对 API

数仓工程师改写电表日结的关联 SQL 后，用本服务确认读数**没有漏计或重复计入**。
提交两条只读 `SELECT`，服务在同一已提交快照上并行读取两侧结果，按
**行内容 + 出现次数**做多重集核对（忽略行序，`NULL` 与空串视为不同），
返回一致结论或最多 20 种差异行及两侧次数。

此外提供 `POST /sql/field-impact`：改动 `readings` / `meters` 源表字段前，
分析一条只读日结 `SELECT` 的哪些**结果列**会受影响，区分**值依赖**与
**筛选 / 连接 / 分组引起的结果集合依赖**，CTE 转名后仍追溯到源字段；
并发 `ALTER TABLE` 时基于一致表结构核对，无法确认就不发布结论。

## 1. 快速启动

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发请求演示）
```

启动后 PostgreSQL 自动执行 `sql/init.sql`，建好 `meters` / `readings`
源表并写入示例数据；文件末尾附有可直接粘贴到 `/docs` 的两条等价示例查询。

停止并清理：

```bash
docker compose down -v
```

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 存活探针，不触碰数据库 |
| `POST` | `/jobs` | 提交核对任务（同时只允许一组运行，占用中返回 `409`） |
| `GET`  | `/jobs` | 列出近期任务（含终态），状态查询始终可用 |
| `GET`  | `/jobs/{job_id}` | 查询单个任务状态与结论 |
| `POST` | `/jobs/{job_id}/cancel` | 请求取消，及时受理 |
| `POST` | `/sql/field-impact` | 源字段改动的结果列影响分析（一致表结构事务内核对） |

任务状态：`pending → running →（cancelling）→ succeeded | failed | cancelled | timeout`。
失败、取消、超时一律 **不发布比较结论**（`result` 为 `null`），仅给出原因。

### 请求示例

```bash
curl -X POST http://localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "left_sql":  "SELECT meter_id::bigint, wh::bigint FROM readings",
  "right_sql": "SELECT meter_id::bigint, wh::bigint FROM readings",
  "name": "daily-settlement-v2"
}'
```

### 结论结构（成功时）

```json
{
  "status": "succeeded",
  "result": {
    "identical": false,
    "columns": ["meter_id", "wh"],
    "column_types": ["bigint", "bigint"],
    "left_row_total": 504013,
    "right_row_total": 504012,
    "matched_group_count": 504011,
    "diff_group_total": 1,
    "diffs_shown": 1,
    "diffs": [
      {"values": [1001, 1150], "left_count": 2, "right_count": 1}
    ]
  }
}
```

## 3. 对提交 SQL 的约束

- 必须是**单条只读 SELECT**（允许 `WITH` / `UNION` / `INTERSECT` / `EXCEPT`），
  静态解析拒绝多语句与任何写操作 / DDL / `SELECT INTO` / `LOCK` / `CALL` 等；
- 结果列只允许 `bigint` 与 `text`，两侧**列数相同、对应位置类型相同**，
  否则本轮失败且不给出比较结论；
- 数据库连接再叠加一层纵深防御：`READ ONLY` 事务、`statement_timeout`、
  `lock_timeout`，即使静态校验被绕过也无法写入或长时间持锁。

## 4. 字段影响分析 `POST /sql/field-impact`

改动 `readings` / `meters` 的源字段前，提交一条只读日结 `SELECT` 与待评估的
源字段，服务返回每个**结果列**对这些字段的两类依赖：

- `value_dependency`（**值依赖**）：源字段直接流入该结果列的取值表达式
  （显式列、列别名、`SUM/COUNT` 的参数），改字段会改变列的值；
- `rowset_dependency`（**结果集合依赖**）：源字段出现在 `WHERE` /
  `JOIN ON` / `GROUP BY` 中，影响的是该列“有哪些行 / 如何分组聚合”，
  原因细分为 `filter` / `join` / `group`；CTE 内的对应原因标记为
  `cte_filter` / `cte_join` / `cte_group`，随 CTE 转名一路追溯到源字段，
  并作用于该 CTE 下游的全部结果列。
- `COUNT(*)` 没有值依赖，只受筛选 / 连接 / 分组的结果集合影响；
- `not_referenced` 列出请求了但整条查询未引用到的源字段。

### 请求与响应示例

```bash
curl -X POST http://localhost:8000/sql/field-impact \
  -H 'Content-Type: application/json' -d '{
    "sql": "WITH d AS (SELECT r.meter_id AS mid, r.wh AS w FROM readings r \
WHERE r.read_time >= TIMESTAMPTZ '\''2026-11-15 00:00:00+00'\'') \
SELECT m.line_name AS line_name, d.mid AS meter_id, SUM(d.w) AS wh_total, COUNT(*) AS cnt \
FROM d INNER JOIN meters m ON m.meter_id = d.mid GROUP BY m.line_name, d.mid",
    "source_fields": ["readings.wh", "readings.read_time", "readings.meter_id",
                      "meters.meter_id", "meters.line_name"]
  }'
```

```json
{
  "requested_fields": ["readings.wh", "readings.read_time", "..."],
  "affected_output_columns": [
    {
      "output_name": "wh_total",
      "value_dependency": ["readings.wh"],
      "rowset_dependency": [
        {"field": "readings.meter_id", "reason": "join",  "detail": "内连接条件"},
        {"field": "readings.meter_id", "reason": "group", "detail": "GROUP BY 分组键"},
        {"field": "readings.read_time", "reason": "cte_filter",
         "detail": "CTE d 的 WHERE 筛选条件"}
      ]
    }
  ],
  "not_referenced": []
}
```

### 支持的语法（范围外明确拒绝，不猜测）

- 单条只读 `SELECT`，**单层 `WITH`**：CTE 之间可前后引用、可链式转名，
  但拒绝递归 CTE、嵌套 WITH、CTE 内子查询；
- `FROM` 仅允许 `readings` / `meters` 基表或已定义 CTE，仅允许
  **内连接（`INNER JOIN ... ON`）**；拒绝自连接 / 外连接 / `CROSS` /
  `NATURAL` / `USING` / `LATERAL` / 子查询作关系；
- 投影仅允许显式列（可表别名限定、可括号 / `CAST`）、列别名，以及
  `SUM(列)` / `COUNT(列)` / `COUNT(*)`；拒绝 `DISTINCT`、其它聚合与函数、
  运算表达式；
- 支持 `WHERE`（`AND/OR/NOT`、比较、`BETWEEN/IN/IS NULL/LIKE`）与
  仅列引用的 `GROUP BY`（拒绝序号分组）；`ORDER BY` 允许但不参与血缘；
- 拒绝 `SELECT *`、`HAVING`、`LIMIT/OFFSET`、集合操作、窗口函数、
  标量子查询等。未知列、未知表、裸列歧义、重复结果列名一律 `400` 拒绝。

### 并发 ALTER：一致表结构事务，无法确认就不发布

字段解析与 SQL 有效性核对在**同一个 `REPEATABLE READ READ ONLY` 事务**内完成：

1. 先对 `readings` / `meters` 加 `ACCESS SHARE` 锁，与并发 `ALTER TABLE`
   的 `ACCESS EXCLUSIVE` 锁互斥——未提交的 ALTER 会让本请求在锁队列等待，
   等锁（`lock_timeout`）超时返回 `409`，**不发布影响结论**；
2. 锁获取后在同一事务 / 同一快照内读取 `information_schema` 目录；
3. 用 `EXPLAIN`（不实际执行、无副作用）在该结构下核对 SQL 有效性，
   未知表 / 未知列 / 歧义列返回 `422`，不发布结论；
4. 事务末重读目录并比对结构指纹，前后不一致同样 `409` 拒绝发布。

因此分析所用的名称解析依据与“查询是否有效”的判断严格基于同一版表结构；
已提交的 ALTER 对后续请求整体可见（新列可解析、被删列判无效）。

## 5. 关键设计

### 同一已提交快照，源表仍可更正

每条任务开三条独立连接：快照连接执行
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` + `pg_export_snapshot()`，
两条读取连接分别 `SET TRANSACTION SNAPSHOT` 绑定**同一个快照**。
核对期间其他会话对 `readings` / `meters` 的更正照常提交，但对本轮不可见。

### 内存有界的外部归并（不全量载入、不靠哈希判等）

1. 命名服务端游标按 `itersize` 分批拉取，客户端从不缓存整表；
2. 每 `SORT_CHUNK_ROWS`（默认 2 万）行在进程内排序，逐行流式 pickle 落盘为一个 run；
3. 归并阶段对两侧 run 做单轮 k 路堆归并（堆中每流只驻留一行），
   键随行走，按全序键聚合等值组后比较两侧次数；
4. 差异明细最多保留 20 种，但总行数、差异组总数等计数覆盖全量数据；
5. 全程不使用整表哈希作为判等依据。

### 单名额、30 秒时限与终止语义

- 进程内单名额信号量：全局同时只运行一组，冲突返回 `409`；
- 看门狗按 `JOB_TIMEOUT_SECONDS`（默认 30）判定超时；
- 取消/超时/任一侧失败后：`conn.cancel()` 请求中止本轮全部 SQL，
  必要时 `pg_terminate_backend` 兜底；
- 严格按顺序收尾：**中止 SQL → 关闭连接 → 清空暂存目录 → 释放名额 → 才落终态**，
  因此状态查询一旦看到终态，名额必然已经释放、暂存必然已经清空。

## 6. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PG_DSN` | `postgresql://recon:recon@postgres:5432/recon` | 数据库连接串 |
| `JOB_TIMEOUT_SECONDS` | `30` | 单轮硬时限 |
| `PG_STATEMENT_TIMEOUT_MS` | `28000` | 服务端单语句超时（留 2s 收尾余量） |
| `PG_LOCK_TIMEOUT_MS` | `2000` | 字段影响分析等待一致表结构的锁超时 |
| `SORT_CHUNK_ROWS` | `20000` | 单个排序块行数 |
| `MAX_DIFF_TYPES` | `20` | 差异展示上限 |
| `WORK_DIR` | `/tmp/recon-work` | 外部排序暂存目录（独立卷） |

## 7. 本地自测

```bash
# 单元 + 集成测试（集成测试需要一个 PostgreSQL；可用 PG_DSN 指定）
python -m pytest tests/ -q

# 50 万行级端到端压测（内存峰值、暂存占用、结论正确性、清理与名额释放）
python scripts/load_test.py
```

实测（受限沙箱环境，容器内通常更快）：两侧各 504,012 行、
含一次重复计入差异，端到端约 16 秒，进程峰值 RSS 约 68 MiB，
暂存峰值约 44 MiB，结束后暂存目录清空、名额释放。

## 8. 目录结构

```
app/
  config.py          运行期配置
  sqlvalidation.py   只读 SELECT 静态校验
  field_impact.py    字段影响静态血缘（值依赖/结果集合依赖，CTE 转名追溯）
  db.py              快照会话、只读连接、命名游标流式拉取、列类型闸门、
                     字段影响分析的一致表结构事务（锁 + 目录 + EXPLAIN + 指纹）
  external_merge.py  流式 run、k 路归并、多重集核对
  manager.py         单名额编排、看门狗、取消/超时/失败的统一收尾
  models.py          API 模型
  main.py            FastAPI 路由
sql/init.sql         源表、示例数据、示例查询
scripts/             压测数据生成与负载脚本
tests/               单元与集成测试（含字段影响分析与并发 ALTER 一致性）
docker-compose.yml   应用（128MiB 上限）+ PostgreSQL
Dockerfile
```
