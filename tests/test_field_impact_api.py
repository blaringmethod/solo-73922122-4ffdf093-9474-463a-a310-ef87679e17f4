"""POST /sql/field-impact 的端到端测试（FastAPI + 真实 PostgreSQL）。"""
from __future__ import annotations

import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

VALID_SQL = (
    "WITH d AS ("
    "  SELECT r.meter_id AS mid, r.wh AS w FROM readings r "
    "  WHERE r.read_time >= TIMESTAMPTZ '2026-11-15 00:00:00+00'"
    ") "
    "SELECT m.line_name AS line_name, d.mid AS meter_id, "
    "SUM(d.w) AS wh_total, COUNT(*) AS cnt "
    "FROM d INNER JOIN meters m ON m.meter_id = d.mid "
    "GROUP BY m.line_name, d.mid"
)
ALL_FIELDS = [
    "readings.meter_id",
    "readings.read_time",
    "readings.wh",
    "meters.meter_id",
    "meters.line_name",
]


def test_field_impact_happy_path(client: TestClient) -> None:
    resp = client.post(
        "/sql/field-impact",
        json={"sql": VALID_SQL, "source_fields": ALL_FIELDS},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_fields"] == ALL_FIELDS
    names = {c["output_name"] for c in body["affected_output_columns"]}
    assert names == {"line_name", "meter_id", "wh_total", "cnt"}

    by_name = {c["output_name"]: c for c in body["affected_output_columns"]}
    assert by_name["wh_total"]["value_dependency"] == ["readings.wh"]
    assert by_name["meter_id"]["value_dependency"] == ["readings.meter_id"]
    assert by_name["line_name"]["value_dependency"] == ["meters.line_name"]
    assert by_name["cnt"]["value_dependency"] == []

    # CTE 内 WHERE 的 read_time 经转名追溯为 cte_filter，覆盖全部输出列
    for col in body["affected_output_columns"]:
        reasons = {(r["field"], r["reason"]) for r in col["rowset_dependency"]}
        assert ("readings.read_time", "cte_filter") in reasons
        assert ("readings.meter_id", "join") in reasons
        assert ("readings.meter_id", "group") in reasons
    assert body["not_referenced"] == []


def test_field_impact_not_referenced(client: TestClient) -> None:
    resp = client.post(
        "/sql/field-impact",
        json={
            "sql": "SELECT meter_id FROM readings",
            "source_fields": ["readings.meter_id", "readings.wh"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["not_referenced"] == ["readings.wh"]
    assert body["affected_output_columns"][0]["output_name"] == "meter_id"


def test_field_impact_rejects_write_sql(client: TestClient) -> None:
    resp = client.post(
        "/sql/field-impact",
        json={"sql": "DROP TABLE readings", "source_fields": ALL_FIELDS},
    )
    assert resp.status_code == 400


def test_field_impact_rejects_unknown_column_422(client: TestClient) -> None:
    # 静态语法合法，但在当前一致表结构下不存在该列 -> 422，不发布结论
    resp = client.post(
        "/sql/field-impact",
        json={"sql": "SELECT bogus_col FROM readings", "source_fields": ALL_FIELDS},
    )
    assert resp.status_code == 422
    assert "不存在的列" in resp.json()["detail"]["message"]


def test_field_impact_rejects_unknown_requested_field_400(client: TestClient) -> None:
    resp = client.post(
        "/sql/field-impact",
        json={"sql": "SELECT meter_id FROM readings", "source_fields": ["readings.nope"]},
    )
    assert resp.status_code == 400


def test_field_impact_rejects_out_of_scope_syntax_400(client: TestClient) -> None:
    resp = client.post(
        "/sql/field-impact",
        json={
            "sql": "SELECT DISTINCT meter_id FROM readings",
            "source_fields": ALL_FIELDS,
        },
    )
    assert resp.status_code == 400


def test_jobs_endpoints_still_available(client: TestClient) -> None:
    # /jobs 保留：健康检查与任务列表仍可用
    assert client.get("/health").status_code == 200
    assert client.get("/jobs").status_code == 200
    sql = "SELECT meter_id::bigint AS meter_id FROM readings"
    created = client.post("/jobs", json={"left_sql": sql, "right_sql": sql})
    assert created.status_code == 201


def test_concurrent_alter_lock_then_release_gives_consistent_conclusion(
    client: TestClient, pg_dsn: str
) -> None:
    """另一连接持有 ACCESS EXCLUSIVE 锁（模拟未提交的 ALTER TABLE）时，
    字段解析的 LOCK 必须在锁队列中等待，直到拿到一致表结构后才发布结论。

    沙箱 seccomp 封死了 PostgreSQL 的信号/定时器，lock_timeout 无法本地触发，
    因此本用例验证“持锁 -> 请求阻塞排队 -> 锁释放 -> 同一结构事务内成功”
    这条确定性路径；超时转 409 的路径由 test_*_409 用 monkeypatch 覆盖。
    """
    holder = psycopg.connect(pg_dsn, autocommit=False)
    result: dict = {}
    try:
        holder.execute("BEGIN")
        holder.execute("LOCK TABLE meters IN ACCESS EXCLUSIVE MODE")

        def call_api() -> None:
            result["resp"] = client.post(
                "/sql/field-impact",
                json={
                    "sql": "SELECT meter_id FROM meters",
                    "source_fields": ["meters.meter_id"],
                },
            )

        t = threading.Thread(target=call_api, daemon=True)
        t.start()
        t.join(timeout=1.5)
        assert t.is_alive(), "持锁期间请求不应返回，必须等待一致表结构"

        holder.rollback()  # 模拟 ALTER 回滚/提交后释放 ACCESS EXCLUSIVE
        t.join(timeout=15)
        assert not t.is_alive(), "锁释放后请求应及时完成"
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert (
            resp.json()["affected_output_columns"][0]["output_name"] == "meter_id"
        )
    finally:
        holder.close()


def test_committed_alter_is_seen_consistently(
    client: TestClient, pg_dsn: str
) -> None:
    """ALTER 已提交后，分析基于改后的同一版表结构；新增列可解析，
    被删列在该结构下无效 -> 422，结论与当前结构严格一致。"""
    admin = psycopg.connect(pg_dsn, autocommit=True)
    try:
        admin.execute("ALTER TABLE meters ADD COLUMN IF NOT EXISTS zone_code text")
        admin.execute("ALTER TABLE meters DROP COLUMN IF EXISTS line_name")
        resp = client.post(
            "/sql/field-impact",
            json={
                "sql": "SELECT zone_code FROM meters",
                "source_fields": ["meters.zone_code"],
            },
        )
        try:
            assert resp.status_code == 200, resp.text
            assert (
                resp.json()["affected_output_columns"][0]["value_dependency"]
                == ["meters.zone_code"]
            )
            # 已删除的列：一致结构核对失败，拒绝发布
            bad = client.post(
                "/sql/field-impact",
                json={
                    "sql": "SELECT line_name FROM meters",
                    "source_fields": ["meters.line_name"],
                },
            )
            assert bad.status_code in (400, 422)
        finally:
            # 还原结构，避免污染其它用例
            admin.execute("ALTER TABLE meters ADD COLUMN IF NOT EXISTS line_name text")
            admin.execute("ALTER TABLE meters DROP COLUMN IF EXISTS zone_code")
            admin.execute(
                "UPDATE meters SET line_name = m.line_name FROM "
                "(VALUES (1001, 'A线'),(1002, 'A线'),(2001, 'B线'),"
                "(2002, 'B线'),(3001, 'C线'),(3002, 'C线')) "
                "AS m(meter_id, line_name) WHERE meters.meter_id = m.meter_id"
            )
    finally:
        admin.close()


def test_schema_unavailable_returns_409_without_conclusion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无法确认一致表结构时（如序列化失败）明确 409 且不返回影响列。"""
    from app import db as db_mod

    def boom(_sql):
        raise db_mod.SchemaUnavailableError("模拟：无法确认一致表结构，本次不发布影响结论")

    monkeypatch.setattr(db_mod, "read_consistent_schema", boom)
    resp = client.post(
        "/sql/field-impact",
        json={"sql": "SELECT meter_id FROM readings", "source_fields": ALL_FIELDS},
    )
    assert resp.status_code == 409
    assert "affected_output_columns" not in resp.json()
