"""字段影响分析（静态血缘，只读 SELECT 限定语法）。

输入一条符合限定语法的只读 SELECT 与 readings / meters 的源字段，输出每个
**结果列**对源字段的两类依赖：

- value_dependency（值依赖）：源字段直接流入该结果列的取值表达式
  （显式列 / 别名 / SUM / COUNT 的参数）；改字段会改变列的值；
- rowset_dependency（结果集合依赖）：源字段出现在 WHERE / JOIN ON / GROUP BY
  中，影响的是该结果列的“有哪些行 / 如何分组聚合”；CTE 内的过滤、连接、
  分组原因会随 CTE 转名一路追溯到源字段。

限定语法（范围外一律明确拒绝，不猜测）：
- 单条只读 SELECT，允许单层 WITH（CTE 之间可前后引用，但不得递归、不得嵌套
  WITH、CTE 内不得再出现子查询）；
- FROM 仅允许 readings / meters 基表或已定义 CTE，仅允许内连接（INNER JOIN），
  拒绝自连接 / 外连接 / CROSS / NATURAL / USING / LATERAL / 子查询作为关系；
- 投影只允许显式列（可带表别名限定、可加括号/CAST）、列别名，以及
  SUM(列) / COUNT(列) / COUNT(*) 聚合（拒绝 DISTINCT 与其他函数/运算表达式）；
- 支持 WHERE（AND/OR/NOT、比较、BETWEEN/IN/IS NULL/LIKE，字面量可 CAST）、
  仅列引用的 GROUP BY（拒绝序号分组）；
- 允许 ORDER BY 但不参与血缘（只影响行序）；拒绝 DISTINCT/HAVING/LIMIT/
  集合操作/窗口函数/标量子查询等其余结构。

名称解析依据调用方在**一致表结构事务**内读到的目录，未知列、歧义列直接拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .db import SOURCE_TABLES, ColumnInfo

# 结果集合依赖的原因类别
REASON_FILTER = "filter"
REASON_JOIN = "join"
REASON_GROUP = "group"
REASON_CTE_FILTER = "cte_filter"
REASON_CTE_JOIN = "cte_join"
REASON_CTE_GROUP = "cte_group"

_CTE_REASON_MAP = {
    REASON_FILTER: REASON_CTE_FILTER,
    REASON_JOIN: REASON_CTE_JOIN,
    REASON_GROUP: REASON_CTE_GROUP,
}

# WHERE / JOIN ON 中允许出现的表达式节点（列引用与字面量之外）
_BOOL_ALLOWED = (
    exp.And, exp.Or, exp.Not, exp.Paren,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.Between, exp.In, exp.Is, exp.Like, exp.ILike,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg,
    exp.Cast, exp.Column, exp.Literal, exp.Null, exp.Boolean,
)


class FieldImpactError(ValueError):
    """字段影响分析无法进行（语法越界 / 列未知或歧义 / 引用对象不支持）。"""


def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    """剥掉纯括号层（sqlglot 30.31 没有 Expression.unwrap）。"""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


@dataclass(frozen=True)
class SourceField:
    table: str
    column: str

    def canonical(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class RowsetReason:
    field: SourceField
    reason: str
    detail: str


@dataclass
class _Source:
    """FROM/JOIN 链上的一个关系（基表或 CTE 实例）。"""
    alias: str                       # 查询内可见的关系别名（未起别名时为关系名）
    kind: str                        # "base" | "cte"
    table: str | None = None         # kind=base 时的基表名
    cte: "_CteInfo | None" = None    # kind=cte 时的 CTE 描述


@dataclass
class _CteInfo:
    name: str
    # CTE 输出列名 -> 该列取值依赖到的源字段集合
    value_map: dict[str, set[SourceField]] = field(default_factory=dict)
    # CTE 内所有过滤/连接/分组引起的结果集合依赖
    rowset_reasons: list[RowsetReason] = field(default_factory=list)


@dataclass
class OutputColumnImpact:
    output_name: str
    value_dependency: list[str]
    rowset_dependency: list[RowsetReason]


@dataclass
class FieldImpactResult:
    output_columns: list[OutputColumnImpact]
    requested_fields: list[str]
    not_referenced: list[str]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def analyze_field_impact(
    sql: str,
    requested: list[str],
    schema: dict[str, list[ColumnInfo]],
) -> FieldImpactResult:
    """按一致表结构 ``schema`` 分析 ``sql`` 对 ``requested`` 源字段的影响。"""
    targets = _parse_requested(requested, schema)
    stmt = _parse_one_select(sql)
    output_columns = _analyze_select(stmt, schema)

    target_set = set(targets)
    not_referenced: set[str] = {t.canonical() for t in targets}
    impacts: list[OutputColumnImpact] = []
    for col in output_columns:
        value_hits = sorted(
            f.canonical() for f in col.value_dependency if f in target_set
        )
        rowset_hits = [
            RowsetReason(field=r.field, reason=r.reason, detail=r.detail)
            for r in col.rowset_dependency
            if r.field in target_set
        ]
        # 去重（保持顺序）
        seen: set[tuple[str, str, str]] = set()
        deduped: list[RowsetReason] = []
        for r in rowset_hits:
            key = (r.field.canonical(), r.reason, r.detail)
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        for name in value_hits:
            not_referenced.discard(name)
        for r in deduped:
            not_referenced.discard(r.field.canonical())
        if value_hits or deduped:
            impacts.append(
                OutputColumnImpact(
                    output_name=col.output_name,
                    value_dependency=value_hits,
                    rowset_dependency=deduped,
                )
            )
    return FieldImpactResult(
        output_columns=impacts,
        requested_fields=[t.canonical() for t in targets],
        not_referenced=sorted(not_referenced),
    )


# ---------------------------------------------------------------------------
# 请求字段与解析
# ---------------------------------------------------------------------------

def _parse_requested(
    requested: list[str], schema: dict[str, list[ColumnInfo]]
) -> list[SourceField]:
    if not requested:
        raise FieldImpactError("至少提供一个源字段")
    targets: list[SourceField] = []
    seen: set[str] = set()
    for raw in requested:
        name = (raw or "").strip().lower()
        parts = [p.strip().strip('"') for p in name.split(".") if p.strip()]
        if len(parts) != 2:
            raise FieldImpactError(
                f"源字段 {raw!r} 必须使用“表名.列名”形式（表名仅限 "
                f"{' / '.join(SOURCE_TABLES)}）"
            )
        table, column = parts
        if table not in SOURCE_TABLES:
            raise FieldImpactError(
                f"源字段 {raw!r} 的表 {table!r} 不在允许范围，"
                f"只支持 {' / '.join(SOURCE_TABLES)}"
            )
        cols = {c.name.lower(): c for c in schema.get(table, [])}
        if column not in cols:
            raise FieldImpactError(
                f"源字段 {raw!r} 在当前表结构中不存在（已按一致表结构核对）"
            )
        sf = SourceField(table=table, column=column)
        key = sf.canonical()
        if key not in seen:
            seen.add(key)
            targets.append(sf)
    return targets


def _parse_one_select(sql: str) -> exp.Select:
    text = (sql or "").strip()
    if not text:
        raise FieldImpactError("SQL 为空")
    if text.endswith(";"):
        text = text[:-1].rstrip()
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise FieldImpactError(f"SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise FieldImpactError("只允许提交单条 SELECT 语句")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise FieldImpactError(
            "只允许单条 SELECT（不支持 UNION / INTERSECT / EXCEPT 等集合操作）"
        )
    return stmt


# ---------------------------------------------------------------------------
# 关系（FROM / JOIN）结构校验与构建
# ---------------------------------------------------------------------------

def _check_no_node(node: exp.Expression, forbidden: tuple[type, ...], message: str) -> None:
    bad = node.find(*forbidden)
    if bad is not None:
        raise FieldImpactError(f"{message}：发现 {type(bad).__name__}")


# 任何位置都不允许的结构（Star / Distinct 需结合上下文单独判定，
# 因为 COUNT(*) 与聚合 DISTINCT 也会产生这两种节点）
_GLOBAL_FORBIDDEN = (
    exp.Subquery, exp.Window, exp.Union, exp.Intersect, exp.Except,
    exp.Exists, exp.Lateral,
)


def _reject_select_distinct(select_node: exp.Select, where: str) -> None:
    if select_node.args.get("distinct") is not None:
        raise FieldImpactError(f"{where} 不支持 SELECT DISTINCT（超出支持的查询语法范围）")


def _analyze_select(
    stmt: exp.Select, schema: dict[str, list[ColumnInfo]]
) -> list[OutputColumnImpact]:
    _check_no_node(stmt, _GLOBAL_FORBIDDEN, "超出支持的查询语法范围")
    _reject_select_distinct(stmt, "查询")
    # SELECT * / t.* 一律拒绝；COUNT(*) 中的 Star 父节点是 Count，属合法
    for star in stmt.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise FieldImpactError("不支持 SELECT *，请显式列出结果列")
    if stmt.args.get("having") is not None:
        raise FieldImpactError("不支持 HAVING（超出支持的查询语法范围）")
    if stmt.args.get("limit") is not None or stmt.args.get("offset") is not None:
        raise FieldImpactError("不支持 LIMIT / OFFSET（超出支持的查询语法范围）")

    ctes = _build_ctes(stmt, schema)
    sources = _build_sources(stmt, schema, ctes)
    scope = _Scope(sources, schema, ctes)

    # 行集合依赖：WHERE / JOIN ON / GROUP BY
    rowset_reasons: list[RowsetReason] = []
    where = stmt.args.get("where")
    if where is not None:
        _check_boolean_expr(where.this, "WHERE")
        for col in where.find_all(exp.Column):
            rowset_reasons.append(
                RowsetReason(
                    field=scope.resolve(col, "WHERE"),
                    reason=REASON_FILTER,
                    detail="WHERE 筛选条件",
                )
            )
    for join in stmt.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            _check_boolean_expr(on, "JOIN ON")
            for col in on.find_all(exp.Column):
                rowset_reasons.append(
                    RowsetReason(
                        field=scope.resolve(col, "JOIN ON"),
                        reason=REASON_JOIN,
                        detail="内连接条件",
                    )
                )

    group_cols: list[exp.Column] = []
    group = stmt.args.get("group")
    if group is not None:
        for gexpr in group.expressions:
            gexpr = _unwrap_paren(gexpr)
            if not isinstance(gexpr, exp.Column):
                raise FieldImpactError("GROUP BY 只允许直接的列引用（不支持序号或表达式）")
            group_cols.append(gexpr)
        for col in group_cols:
            rowset_reasons.append(
                RowsetReason(
                    field=scope.resolve(col, "GROUP BY"),
                    reason=REASON_GROUP,
                    detail="GROUP BY 分组键",
                )
            )

    # 投影 -> 输出列
    # 作用域内 CTE（含其依赖链）的过滤/连接/分组原因影响该 CTE 输出的
    # 每一行，因此对本层所有结果列都是结果集合依赖（CTE 转名追溯）
    inherited = _inherited_cte_reasons(sources)
    outputs: list[OutputColumnImpact] = []
    used_names: set[str] = set()
    aggregate_present = False
    for proj in stmt.expressions:
        name, value_fields, is_agg = _analyze_projection(proj, scope)
        aggregate_present = aggregate_present or is_agg
        lname = name.lower()
        if lname in used_names:
            raise FieldImpactError(f"结果列名 {name!r} 重复，无法唯一标识输出列")
        used_names.add(lname)

        col_rowset: list[RowsetReason] = list(rowset_reasons)
        col_rowset.extend(inherited)
        outputs.append(
            OutputColumnImpact(
                output_name=name,
                value_dependency=sorted(value_fields, key=SourceField.canonical),
                rowset_dependency=col_rowset,
            )
        )

    if aggregate_present and group is None:
        # 无 GROUP BY 的聚合：整张表聚成一行，任何被引用源字段都参与行集合，
        # 原因已随投影值依赖体现；无需额外结构。
        pass
    return outputs


def _inherited_cte_reasons(sources: list[_Source]) -> list[RowsetReason]:
    """收集作用域内各 CTE（同一 CTE 只取一次）携带的结果集合原因，
    并把原因标记为 cte_*；映射幂等，支持 CTE 链上的逐级转名追溯。"""
    out: list[RowsetReason] = []
    seen_names: set[str] = set()
    for src in sources:
        if src.kind != "cte" or src.cte is None:
            continue
        cte = src.cte
        if cte.name in seen_names:
            continue
        seen_names.add(cte.name)
        for r in cte.rowset_reasons:
            out.append(
                RowsetReason(
                    field=r.field,
                    reason=_CTE_REASON_MAP.get(r.reason, r.reason),
                    detail=r.detail,
                )
            )
    return out


# ---------------------------------------------------------------------------
# CTE 构建（单层、可前后引用、禁止递归/嵌套 WITH/内部子查询）
# ---------------------------------------------------------------------------

def _build_ctes(stmt: exp.Select, schema: dict[str, list[ColumnInfo]]) -> dict[str, _CteInfo]:
    with_clause = stmt.args.get("with_")
    ctes: dict[str, _CteInfo] = {}
    if with_clause is None:
        return ctes
    if getattr(with_clause, "recursive", False):
        raise FieldImpactError("不支持 RECURSIVE CTE（超出支持的查询语法范围）")
    for cte in with_clause.expressions:
        if not isinstance(cte, exp.CTE):
            raise FieldImpactError("WITH 子句只允许普通 CTE 定义")
        name = cte.alias
        if not name:
            raise FieldImpactError("CTE 必须显式命名")
        lname = name.lower()
        if lname in ctes:
            raise FieldImpactError(f"CTE 名 {name!r} 重复")
        if lname in SOURCE_TABLES:
            raise FieldImpactError(f"CTE 名 {name!r} 与源表同名，拒绝以避免语义歧义")
        body = cte.this
        if not isinstance(body, exp.Select):
            raise FieldImpactError("CTE 主体必须是单个 SELECT")
        if body.args.get("with_") is not None:
            raise FieldImpactError("不支持嵌套 WITH（仅允许单层 CTE）")
        _check_no_node(
            body,
            (exp.Subquery, exp.Window, exp.Union, exp.Intersect, exp.Except,
             exp.Exists, exp.Lateral),
            "CTE 内出现超出支持范围的语法",
        )
        _reject_select_distinct(body, "CTE")
        # SELECT * / t.* 只允许出现在 COUNT(*) 里，投影层单独拦截
        for star in body.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise FieldImpactError(f"CTE {name} 不支持 SELECT *，请显式列出列名")
        if body.args.get("having") is not None or body.args.get("limit") is not None:
            raise FieldImpactError("CTE 内不支持 HAVING / LIMIT")
        ctes[lname] = _analyze_cte_body(name, body, schema, ctes)
    return ctes


def _analyze_cte_body(
    name: str,
    body: exp.Select,
    schema: dict[str, list[ColumnInfo]],
    prior_ctes: dict[str, _CteInfo],
) -> _CteInfo:
    sources = _build_sources(body, schema, prior_ctes)
    scope = _Scope(sources, schema, prior_ctes)
    info = _CteInfo(name=name)

    where = body.args.get("where")
    if where is not None:
        _check_boolean_expr(where.this, "CTE WHERE")
        for col in where.find_all(exp.Column):
            info.rowset_reasons.append(
                RowsetReason(
                    field=scope.resolve(col, "CTE WHERE"),
                    reason=REASON_FILTER,
                    detail=f"CTE {name} 的 WHERE 筛选条件",
                )
            )
    for join in body.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            _check_boolean_expr(on, "CTE JOIN ON")
            for col in on.find_all(exp.Column):
                info.rowset_reasons.append(
                    RowsetReason(
                        field=scope.resolve(col, "CTE JOIN ON"),
                        reason=REASON_JOIN,
                        detail=f"CTE {name} 的内连接条件",
                    )
                )
    group = body.args.get("group")
    if group is not None:
        for gexpr in group.expressions:
            gexpr = _unwrap_paren(gexpr)
            if not isinstance(gexpr, exp.Column):
                raise FieldImpactError("CTE 的 GROUP BY 只允许直接的列引用")
        for col in group.find_all(exp.Column):
            info.rowset_reasons.append(
                RowsetReason(
                    field=scope.resolve(col, "CTE GROUP BY"),
                    reason=REASON_GROUP,
                    detail=f"CTE {name} 的 GROUP BY 分组键",
                )
            )

    used_names: set[str] = set()
    for proj in body.expressions:
        out_name, value_fields, _ = _analyze_projection(proj, scope)
        if out_name.lower() in used_names:
            raise FieldImpactError(f"CTE {name} 的输出列名 {out_name!r} 重复")
        used_names.add(out_name.lower())
        info.value_map[out_name.lower()] = set(value_fields)

    # 依赖到的前序 CTE 的内部原因同样随本 CTE 的结果集流出（链式转名追溯）
    info.rowset_reasons.extend(_inherited_cte_reasons(sources))
    return info


# ---------------------------------------------------------------------------
# FROM / JOIN
# ---------------------------------------------------------------------------

def _build_sources(
    select_node: exp.Select,
    schema: dict[str, list[ColumnInfo]],
    ctes: dict[str, _CteInfo],
) -> list[_Source]:
    from_clause = select_node.args.get("from_")
    if from_clause is None or not isinstance(from_clause.this, exp.Table):
        raise FieldImpactError("FROM 子句必须直接引用源表或 CTE（不支持子查询）")
    sources: list[_Source] = []
    seen_aliases: set[str] = set()
    seen_base_tables: set[str] = set()
    seen_cte_names: set[str] = set()

    def add_table(table_node: exp.Table, where: str) -> _Source:
        if table_node.args.get("db") is not None or table_node.args.get("catalog") is not None:
            raise FieldImpactError(f"{where} 不允许带 schema/catalog 限定的表名")
        rel_name = table_node.name
        if not rel_name:
            raise FieldImpactError(f"{where} 存在无法识别的关系")
        lrel = rel_name.lower()
        alias = (table_node.alias or rel_name)
        lalias = alias.lower()
        if lalias in seen_aliases:
            raise FieldImpactError(f"关系别名 {alias!r} 重复")
        seen_aliases.add(lalias)
        if lrel in ctes:
            if lrel in SOURCE_TABLES:  # 已在 CTE 构建处拦截，双保险
                raise FieldImpactError(f"CTE 名 {rel_name!r} 与源表同名")
            if lrel in seen_cte_names:
                raise FieldImpactError(
                    f"不支持对同一 CTE {rel_name} 的多次引用/自连接"
                    "（超出支持的查询语法范围）"
                )
            seen_cte_names.add(lrel)
            src = _Source(alias=lalias, kind="cte", cte=ctes[lrel])
        elif lrel in SOURCE_TABLES:
            if lrel in seen_base_tables:
                raise FieldImpactError(
                    f"不支持对源表 {lrel} 的自连接/多次引用（超出支持的查询语法范围）"
                )
            seen_base_tables.add(lrel)
            if lrel not in schema:
                raise FieldImpactError(f"源表 {lrel} 在当前表结构中不存在")
            src = _Source(alias=lalias, kind="base", table=lrel)
        else:
            raise FieldImpactError(
                f"{where} 引用的关系 {rel_name!r} 不是 readings/meters 源表或已定义的 CTE"
            )
        sources.append(src)
        return src

    add_table(from_clause.this, "FROM")
    for join in select_node.args.get("joins") or []:
        if join.args.get("using"):
            raise FieldImpactError("不支持 USING 连接，请改写为 ON 等值条件")
        # 仅允许普通内连接：side 为空（非 LEFT/RIGHT/FULL），
        # kind 为空或 INNER，method 为空（非 NATURAL 等）
        if join.side:
            raise FieldImpactError(
                f"不支持 {join.side} 外连接，仅支持内连接（INNER JOIN ... ON）"
            )
        if join.kind and join.kind.upper() != "INNER":
            raise FieldImpactError(
                f"不支持 {join.kind} 连接，仅支持内连接（INNER JOIN ... ON）"
            )
        if join.method:
            raise FieldImpactError("不支持 NATURAL 等连接方法，请改写为显式 INNER JOIN ON")
        if not isinstance(join.this, exp.Table):
            raise FieldImpactError("JOIN 的关系必须是源表或 CTE（不支持子查询 / LATERAL）")
        on = join.args.get("on")
        if not isinstance(on, exp.Expression):
            raise FieldImpactError("内连接必须显式给出 ON 条件")
        add_table(join.this, "JOIN")
    return sources


# ---------------------------------------------------------------------------
# 名称解析
# ---------------------------------------------------------------------------

class _Scope:
    def __init__(
        self,
        sources: list[_Source],
        schema: dict[str, list[ColumnInfo]],
        ctes: dict[str, _CteInfo],
    ) -> None:
        self.sources = sources
        self.schema = schema
        self.ctes = ctes

    def resolve(self, col: exp.Column, clause: str) -> SourceField:
        col_name = col.name.lower()
        qualifier = col.table
        db = col.args.get("db")
        if db is not None:
            raise FieldImpactError(f"{clause} 中不允许 schema 限定列：{col.sql()}")
        if qualifier:
            q = qualifier.lower()
            matches = [s for s in self.sources if s.alias == q]
            if not matches:
                raise FieldImpactError(
                    f"{clause} 中的表别名 {qualifier!r} 未在 FROM/JOIN 中定义"
                )
            return self._resolve_in_source(matches[0], col_name, col.sql(), clause)
        # 裸列：在所有可见关系中查找，必须唯一
        candidates: list[SourceField] = []
        for src in self.sources:
            sf = self._try_resolve(src, col_name)
            if sf is not None:
                candidates.append(sf)
        if not candidates:
            raise FieldImpactError(f"{clause} 中的列 {col.name!r} 在当前表结构中不存在")
        if len(candidates) > 1:
            raise FieldImpactError(
                f"{clause} 中的裸列 {col.name!r} 同时存在于多个关系，存在歧义，"
                "请用“表别名.列名”显式限定"
            )
        return candidates[0]

    def _try_resolve(self, src: _Source, col_name: str) -> SourceField | None:
        if src.kind == "base":
            cols = {c.name.lower() for c in self.schema[src.table]}  # type: ignore[index]
            if col_name in cols:
                return SourceField(table=src.table, column=col_name)  # type: ignore[arg-type]
            return None
        assert src.cte is not None
        if col_name in src.cte.value_map:
            # CTE 列可能由多个源字段组成；取歧义判断时返回第一个，
            # 真正取值展开走 expand_cte_column。裸列唯一性由调用方保证。
            vals = src.cte.value_map[col_name]
            return next(iter(vals)) if vals else None
        return None

    def _resolve_in_source(
        self, src: _Source, col_name: str, raw: str, clause: str
    ) -> SourceField:
        if src.kind == "base":
            cols = {c.name.lower() for c in self.schema[src.table]}  # type: ignore[index]
            if col_name not in cols:
                raise FieldImpactError(
                    f"{clause} 中的列 {raw!r} 在源表 {src.table} 上不存在"
                )
            return SourceField(table=src.table, column=col_name)  # type: ignore[arg-type]
        assert src.cte is not None
        if col_name not in src.cte.value_map:
            raise FieldImpactError(
                f"{clause} 中的列 {raw!r} 在 CTE {src.cte.name} 的输出列中不存在"
            )
        vals = src.cte.value_map[col_name]
        if len(vals) != 1:
            # 理论上限定语法内单列只映射确定字段；多字段只可能来自异常结构
            raise FieldImpactError(f"{clause} 中的 CTE 列 {raw!r} 无法唯一追溯到源字段")
        return next(iter(vals))

    def expand_cte_column(self, src: _Source, col_name: str) -> set[SourceField]:
        assert src.kind == "cte" and src.cte is not None
        if col_name not in src.cte.value_map:
            raise FieldImpactError(
                f"CTE {src.cte.name} 中不存在输出列 {col_name!r}"
            )
        return set(src.cte.value_map[col_name])


# ---------------------------------------------------------------------------
# 投影表达式
# ---------------------------------------------------------------------------

def _analyze_projection(
    proj: exp.Expression,
    scope: _Scope,
) -> tuple[str, set[SourceField], bool]:
    """返回 (输出列名, 源字段值依赖集合, 是否聚合)。"""
    explicit_alias = proj.alias if isinstance(proj, exp.Alias) else ""
    node = proj.this if isinstance(proj, exp.Alias) else proj
    node = _unwrap_paren(node)

    if isinstance(node, exp.Cast):
        # CAST 不改变血缘，剥掉后继续（CAST(列 AS ...) 合法）
        node = _unwrap_paren(node.this)

    is_agg = False
    if isinstance(node, (exp.Sum, exp.Count)):
        is_agg = True
        fields = _analyze_aggregate(node, scope)
    elif isinstance(node, exp.Column):
        fields = _expand_column(node, scope)
    elif isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        fields = set()
    else:
        if isinstance(node, exp.AggFunc):
            raise FieldImpactError(
                f"聚合函数 {type(node).__name__.upper()} 不在支持范围，仅支持 SUM / COUNT"
            )
        if isinstance(node, exp.Func):
            raise FieldImpactError(
                f"函数 {node.sql()[:40]} 不在支持范围，投影仅支持显式列与 SUM/COUNT"
            )
        raise FieldImpactError(
            f"投影表达式 {node.sql()[:40]!r} 超出支持范围"
            "（仅支持显式列、列别名与 SUM/COUNT 聚合）"
        )

    # 未显式别名时按 PostgreSQL 的默认结果列名补齐，保证输出列可唯一标识
    if explicit_alias:
        alias = explicit_alias
    elif isinstance(node, exp.Column):
        alias = node.name
    elif isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
        alias = "count"
    elif isinstance(node, exp.Count):
        alias = "count"
    elif isinstance(node, exp.Sum):
        alias = "sum"
    else:
        raise FieldImpactError("非纯列引用的结果列必须显式指定列别名")
    return alias, fields, is_agg


def _analyze_aggregate(node: exp.AggFunc, scope: _Scope) -> set[SourceField]:
    if isinstance(node, exp.Count):
        inner = node.this
        if isinstance(inner, exp.Star):
            return set()  # COUNT(*)：无值依赖，仅结果集合依赖
        if isinstance(inner, exp.Paren):
            inner = _unwrap_paren(inner)
        if isinstance(inner, exp.Distinct):
            raise FieldImpactError("不支持 COUNT(DISTINCT ...)（超出支持的查询语法范围）")
        if isinstance(inner, exp.Column):
            return _expand_column(inner, scope)
        raise FieldImpactError("COUNT 仅支持 COUNT(列) 或 COUNT(*)")
    # SUM
    inner = node.this
    if isinstance(inner, exp.Paren):
        inner = _unwrap_paren(inner)
    if isinstance(inner, exp.Distinct):
        raise FieldImpactError("不支持 SUM(DISTINCT ...)（超出支持的查询语法范围）")
    if isinstance(inner, exp.Cast):
        inner = _unwrap_paren(inner.this)
    if isinstance(inner, exp.Column):
        return _expand_column(inner, scope)
    raise FieldImpactError("SUM 仅支持 SUM(列)，不支持表达式参数")


def _expand_column(col: exp.Column, scope: _Scope) -> set[SourceField]:
    """列引用 -> 源字段集合；CTE 转名列在此展开到源字段。"""
    qualifier = col.table
    col_name = col.name.lower()
    if qualifier:
        q = qualifier.lower()
        matches = [s for s in scope.sources if s.alias == q]
        if not matches:
            raise FieldImpactError(f"表别名 {qualifier!r} 未在 FROM/JOIN 中定义")
        src = matches[0]
        if src.kind == "base":
            return {scope.resolve(col, "投影")}
        return scope.expand_cte_column(src, col_name)
    # 裸列
    candidates: list[set[SourceField]] = []
    for src in scope.sources:
        if src.kind == "base":
            cols = {c.name.lower() for c in scope.schema[src.table]}  # type: ignore[index]
            if col_name in cols:
                candidates.append({SourceField(table=src.table, column=col_name)})  # type: ignore[arg-type]
        else:
            assert src.cte is not None
            if col_name in src.cte.value_map:
                candidates.append(scope.expand_cte_column(src, col_name))
    if not candidates:
        raise FieldImpactError(f"投影中的列 {col.name!r} 在当前表结构中不存在")
    if len(candidates) > 1:
        raise FieldImpactError(
            f"投影中的裸列 {col.name!r} 同时存在于多个关系，存在歧义，"
            "请用“表别名.列名”显式限定"
        )
    return set(candidates[0])


# ---------------------------------------------------------------------------
# 布尔表达式白名单校验（WHERE / JOIN ON）
# ---------------------------------------------------------------------------

def _check_boolean_expr(node: exp.Expression, clause: str) -> None:
    # Identifier / DataType / Column 的 Table 标识符等是结构性叶子，不单独判定
    structural = (exp.Identifier, exp.DataType, exp.ColumnDef)
    for sub in node.walk():
        if isinstance(sub, structural):
            continue
        if not isinstance(sub, _BOOL_ALLOWED):
            raise FieldImpactError(
                f"{clause} 中出现不支持的表达式 {type(sub).__name__}"
                f"（{sub.sql()[:30]}），仅允许列、字面量与 AND/OR/NOT、比较、"
                "BETWEEN/IN/IS NULL/LIKE 组合"
            )
    # 白名单内的 Column 合法性由调用方统一通过 scope.resolve 校验
