"""API 请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal[
    "pending",      # 已受理，等待运行名额
    "running",      # 运行中
    "cancelling",   # 已收到取消请求，正在中止本轮 SQL
    "succeeded",    # 终态：核对完成（可能一致，也可能有差异）
    "failed",       # 终态：任一侧失败
    "cancelled",    # 终态：用户取消
    "timeout",      # 终态：超过单轮时限
]

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
ACTIVE_STATUSES = {"pending", "running", "cancelling"}


class CreateJobRequest(BaseModel):
    left_sql: str = Field(..., min_length=1, description="左侧只读 SELECT（如改写后的日结 SQL）")
    right_sql: str = Field(..., min_length=1, description="右侧只读 SELECT（基准 SQL）")
    name: str | None = Field(None, max_length=128, description="可选备注名")


class DiffRow(BaseModel):
    # 与结果集列顺序对齐；NULL 用 null 表示，空串为 ""，二者不同
    values: list[Any]
    left_count: int
    right_count: int


class ReconcileResult(BaseModel):
    identical: bool
    columns: list[str]
    column_types: list[str]          # bigint | text
    left_row_total: int
    right_row_total: int
    matched_group_count: int         # 行内容与次数都相同的去重行数
    diff_group_total: int            # 存在差异的去重行种类总数（不受 20 条展示上限影响）
    diffs_shown: int
    diffs: list[DiffRow]             # 最多 20 种


class JobOut(BaseModel):
    job_id: str
    name: str | None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline: datetime | None
    cancel_requested: bool
    error: str | None
    result: ReconcileResult | None

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# POST /sql/field-impact 字段影响分析
# ---------------------------------------------------------------------------

class FieldImpactRequest(BaseModel):
    sql: str = Field(
        ..., min_length=1,
        description="一条只读 SELECT（限定语法：单层 CTE、内连接、WHERE、GROUP BY、SUM/COUNT）",
    )
    source_fields: list[str] = Field(
        ..., min_length=1,
        description="待评估的源字段，使用“表名.列名”，表名仅限 readings / meters",
    )


class RowsetDependency(BaseModel):
    field: str = Field(..., description="追溯到的源字段，表名.列名")
    reason: Literal[
        "filter", "join", "group",
        "cte_filter", "cte_join", "cte_group",
    ]
    detail: str


class AffectedOutputColumn(BaseModel):
    output_name: str
    value_dependency: list[str] = Field(
        default_factory=list,
        description="值依赖：源字段直接流入该结果列取值表达式",
    )
    rowset_dependency: list[RowsetDependency] = Field(
        default_factory=list,
        description="结果集合依赖：筛选/连接/分组（含 CTE 转名追溯）影响行集合",
    )


class FieldImpactResponse(BaseModel):
    sql: str
    requested_fields: list[str]
    affected_output_columns: list[AffectedOutputColumn]
    not_referenced: list[str] = Field(
        default_factory=list,
        description="请求了但整条查询未引用到的源字段",
    )
