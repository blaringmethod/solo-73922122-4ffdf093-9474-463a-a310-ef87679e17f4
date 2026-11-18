"""字段影响静态血缘分析的单元测试（不依赖数据库，目录用桩）。"""
from __future__ import annotations

import pytest

from app.db import ColumnInfo
from app.field_impact import (
    FieldImpactError,
    analyze_field_impact,
)

SCHEMA = {
    "meters": [
        ColumnInfo("meter_id", "bigint", False),
        ColumnInfo("line_name", "text", False),
    ],
    "readings": [
        ColumnInfo("meter_id", "bigint", False),
        ColumnInfo("read_time", "timestamp with time zone", False),
        ColumnInfo("wh", "bigint", False),
    ],
}
ALL_FIELDS = [
    "readings.meter_id",
    "readings.read_time",
    "readings.wh",
    "meters.meter_id",
    "meters.line_name",
]


def _by_name(result) -> dict:
    return {c.output_name: c for c in result.output_columns}


def test_explicit_columns_and_where_filter() -> None:
    res = analyze_field_impact(
        "SELECT r.meter_id AS mid, r.wh AS w FROM readings r WHERE r.wh > 0",
        ALL_FIELDS,
        SCHEMA,
    )
    cols = _by_name(res)
    assert cols["mid"].value_dependency == ["readings.meter_id"]
    assert cols["w"].value_dependency == ["readings.wh"]
    # WHERE 中的 wh 对每个输出列都是筛选型结果集合依赖
    for col in cols.values():
        reasons = {(r.field.canonical(), r.reason) for r in col.rowset_dependency}
        assert ("readings.wh", "filter") in reasons
    assert set(res.not_referenced) == {
        "readings.read_time",
        "meters.meter_id",
        "meters.line_name",
    }


def test_inner_join_group_and_aggregates() -> None:
    sql = (
        "SELECT m.line_name AS line, SUM(r.wh) AS total, "
        "COUNT(r.meter_id) AS cnt, COUNT(*) AS stars "
        "FROM readings r INNER JOIN meters m ON r.meter_id = m.meter_id "
        "GROUP BY m.line_name"
    )
    res = analyze_field_impact(sql, ALL_FIELDS, SCHEMA)
    cols = _by_name(res)
    assert cols["line"].value_dependency == ["meters.line_name"]
    assert cols["total"].value_dependency == ["readings.wh"]
    assert cols["cnt"].value_dependency == ["readings.meter_id"]
    assert cols["stars"].value_dependency == []  # COUNT(*) 无值依赖

    for col in cols.values():
        reasons = {(r.field.canonical(), r.reason) for r in col.rowset_dependency}
        assert ("readings.meter_id", "join") in reasons
        assert ("meters.meter_id", "join") in reasons
        assert ("meters.line_name", "group") in reasons
    # COUNT(*) 仍受连接/分组的结果集合影响
    star_reasons = {r.field.canonical() for r in cols["stars"].rowset_dependency}
    assert "readings.meter_id" in star_reasons


def test_cte_rename_traces_back_to_source_fields() -> None:
    sql = (
        "WITH d AS ("
        "  SELECT r.meter_id AS mid, r.wh AS w FROM readings r "
        "  WHERE r.read_time >= TIMESTAMPTZ '2026-11-15 00:00:00+00'"
        ") "
        "SELECT d.mid AS meter_id, SUM(d.w) AS wh_total "
        "FROM d JOIN meters m ON m.meter_id = d.mid GROUP BY d.mid"
    )
    res = analyze_field_impact(sql, ALL_FIELDS, SCHEMA)
    cols = _by_name(res)
    # 转名后仍追溯到源字段
    assert cols["meter_id"].value_dependency == ["readings.meter_id"]
    assert cols["wh_total"].value_dependency == ["readings.wh"]
    for col in cols.values():
        reasons = {(r.field.canonical(), r.reason) for r in col.rowset_dependency}
        # CTE 内的 WHERE 经转名追溯为 cte_filter，且作用于全部输出列
        assert ("readings.read_time", "cte_filter") in reasons
        assert ("readings.meter_id", "join") in reasons
        assert ("readings.meter_id", "group") in reasons
    # meters.line_name 在该查询中确实未被引用
    assert res.not_referenced == ["meters.line_name"]


def test_cte_chain_propagates_inner_reasons() -> None:
    sql = (
        "WITH a AS (SELECT meter_id, wh FROM readings WHERE wh > 0), "
        "b AS (SELECT meter_id AS mid, wh AS w FROM a WHERE meter_id < 9999) "
        "SELECT mid, SUM(w) AS s FROM b GROUP BY mid"
    )
    res = analyze_field_impact(sql, ALL_FIELDS, SCHEMA)
    cols = _by_name(res)
    assert cols["s"].value_dependency == ["readings.wh"]
    reasons = {(r.field.canonical(), r.reason) for r in cols["s"].rowset_dependency}
    assert ("readings.wh", "cte_filter") in reasons
    assert ("readings.meter_id", "cte_filter") in reasons
    assert ("readings.meter_id", "group") in reasons


def test_unaliased_aggregate_gets_default_output_name() -> None:
    res = analyze_field_impact(
        "SELECT meter_id AS mid, SUM(wh), COUNT(*) FROM readings GROUP BY meter_id",
        ALL_FIELDS,
        SCHEMA,
    )
    names = {c.output_name for c in res.output_columns}
    assert names == {"mid", "sum", "count"}


def test_cast_and_parenthesized_column_keeps_lineage() -> None:
    res = analyze_field_impact(
        "SELECT wh::text AS w, (meter_id) AS m FROM readings",
        ALL_FIELDS,
        SCHEMA,
    )
    cols = _by_name(res)
    assert cols["w"].value_dependency == ["readings.wh"]
    assert cols["m"].value_dependency == ["readings.meter_id"]


def test_not_referenced_field_reported() -> None:
    res = analyze_field_impact(
        "SELECT meter_id FROM readings",
        ["readings.meter_id", "readings.wh"],
        SCHEMA,
    )
    assert res.not_referenced == ["readings.wh"]
    assert len(res.output_columns) == 1


def test_count_star_with_where_is_rowset_only() -> None:
    res = analyze_field_impact(
        "SELECT COUNT(*) AS c FROM readings WHERE read_time IS NULL",
        ["readings.read_time"],
        SCHEMA,
    )
    col = res.output_columns[0]
    assert col.value_dependency == []
    assert (col.rowset_dependency[0].field.canonical(), col.rowset_dependency[0].reason) == (
        "readings.read_time",
        "filter",
    )


def test_bare_column_ambiguity_rejected() -> None:
    sql = (
        "SELECT meter_id FROM readings r JOIN meters m "
        "ON r.meter_id = m.meter_id"
    )
    with pytest.raises(FieldImpactError, match="歧义"):
        analyze_field_impact(sql, ALL_FIELDS, SCHEMA)


def test_unknown_column_rejected() -> None:
    with pytest.raises(FieldImpactError, match="不存在"):
        analyze_field_impact("SELECT bogus FROM readings", ALL_FIELDS, SCHEMA)


def test_unknown_qualifier_rejected() -> None:
    with pytest.raises(FieldImpactError, match="未在 FROM/JOIN"):
        analyze_field_impact("SELECT x.wh FROM readings r", ALL_FIELDS, SCHEMA)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT meter_id FROM readings UNION SELECT meter_id FROM meters",
        "SELECT * FROM readings",
        "SELECT DISTINCT meter_id FROM readings",
        "SELECT meter_id FROM readings GROUP BY meter_id HAVING count(*) > 1",
        "SELECT meter_id FROM readings LIMIT 5",
        "SELECT r.meter_id FROM readings r LEFT JOIN meters m ON true",
        "SELECT r.meter_id FROM readings r CROSS JOIN meters m",
        "SELECT meter_id FROM readings NATURAL JOIN meters m",
        "SELECT meter_id FROM readings JOIN meters USING (meter_id)",
        "SELECT a.meter_id FROM readings a JOIN readings b ON a.meter_id = b.meter_id",
        "SELECT x.a FROM (SELECT meter_id AS a FROM readings) x",
        "SELECT row_number() OVER (ORDER BY meter_id) AS rn FROM readings",
        "SELECT (SELECT 1) AS z FROM readings",
        "SELECT AVG(wh) FROM readings",
        "SELECT SUM(wh + 1) FROM readings",
        "SELECT SUM(DISTINCT wh) FROM readings",
        "SELECT COUNT(DISTINCT meter_id) FROM readings",
        "SELECT COALESCE(wh, 0) FROM readings",
        "SELECT meter_id FROM readings WHERE EXTRACT(HOUR FROM read_time) > 1",
        "SELECT meter_id FROM readings GROUP BY 1",
        "WITH RECURSIVE t AS (SELECT 1::bigint AS a) SELECT a FROM t",
        "WITH a AS (WITH b AS (SELECT meter_id FROM readings) "
        "SELECT meter_id FROM b) SELECT meter_id FROM a",
        "SELECT meter_id FROM readings; SELECT 1",
        "SELECT a FROM nope",
        "SELECT wh AS x, meter_id AS x FROM readings",
    ],
)
def test_out_of_scope_syntax_rejected(sql: str) -> None:
    with pytest.raises(FieldImpactError):
        analyze_field_impact(sql, ALL_FIELDS, SCHEMA)


def test_bad_requested_field_format_rejected() -> None:
    with pytest.raises(FieldImpactError):
        analyze_field_impact("SELECT meter_id FROM readings", ["readings"], SCHEMA)
    with pytest.raises(FieldImpactError):
        analyze_field_impact("SELECT meter_id FROM readings", ["other.wh"], SCHEMA)
    with pytest.raises(FieldImpactError):
        analyze_field_impact(
            "SELECT meter_id FROM readings", ["readings.bogus"], SCHEMA
        )
    with pytest.raises(FieldImpactError):
        analyze_field_impact("SELECT meter_id FROM readings", [], SCHEMA)
