"""端到端 HTTP 测试：用内存存储替换 app.db 的 PostgreSQL 层。

不依赖真实数据库，但走完整的 FastAPI 路由 + services + domain 流程。
领域算法本身的覆盖见 test_domain.py；本文件侧重 HTTP 状态码、幂等状态码、
错误映射以及 services 的全量重放事务编排。

运行：python -m unittest tests.test_http -v
"""
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from app import db
from app import services
from app.main import create_app


class FakeTx:
    """模拟 psycopg 事务连接：内存表 + 行锁 + 唯一约束。"""

    def __init__(self, state):
        self.state = state

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        st = self.state
        if s.startswith("INSERT INTO cargo"):
            no, start, limit, holder = params
            if no in st["cargo"]:
                from psycopg import errors
                raise errors.UniqueViolation("cargo_no")
            row = {"cargo_no": no, "start_time": start,
                   "temp_upper_limit": limit, "initial_holder": holder,
                   "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            st["cargo"][no] = row
            return _FakeCursor([row])
        if s.startswith("INSERT INTO events"):
            (eid, no, kind, at, temp, fp, tp) = params
            if eid in st["events_by_id"]:
                from psycopg import errors
                raise errors.UniqueViolation("event_id")
            for e in st["events"].values():
                if e["cargo_no"] == no and e["occurred_at"] == at and e["kind"] == kind:
                    from psycopg import errors
                    raise errors.UniqueViolation("partial index")
            row = {"event_id": eid, "cargo_no": no, "kind": kind,
                   "occurred_at": at, "temperature": temp,
                   "from_party": fp, "to_party": tp,
                   "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            st["events"][eid] = row
            st["events_by_id"][eid] = row
            return _FakeCursor([row])
        if "FROM cargo WHERE cargo_no = %s FOR UPDATE" in s:
            row = st["cargo"].get(params[0])
            if row:
                st["locks"].add(params[0])  # 模拟行锁
            return _FakeCursor([row] if row else [])
        if s.startswith("SELECT * FROM cargo WHERE cargo_no"):
            row = st["cargo"].get(params[0])
            return _FakeCursor([row] if row else [])
        if s.startswith("SELECT event_id, cargo_no") and "WHERE event_id" in s:
            row = st["events_by_id"].get(params[0])
            return _FakeCursor([row] if row else [])
        if "FROM events WHERE cargo_no = %s" in s:
            rows = [e for e in st["events"].values()
                    if e["cargo_no"] == params[0]]
            rows.sort(key=lambda r: (r["occurred_at"], r["event_id"]))
            return _FakeCursor(rows)
        if "FROM events ORDER BY" in s:
            rows = list(st["events"].values())
            rows.sort(key=lambda r: (r["occurred_at"], r["event_id"]))
            return _FakeCursor(rows)
        if s.startswith("SELECT cargo_no") and "FROM cargo ORDER BY" in s:
            rows = sorted(st["cargo"].values(),
                          key=lambda r: (r["created_at"], r["cargo_no"]))
            return _FakeCursor(rows)
        if s.startswith("SELECT 1"):
            return _FakeCursor([(1,)])
        raise AssertionError(f"未模拟的 SQL: {s}")

    def commit(self):
        pass

    def rollback(self):
        # 内存实现里写入即时生效；但所有失败路径都会在 INSERT 之前抛错，
        # 故回滚无需撤销（与真实事务“先校验后写入”的顺序一致）。
        pass


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class HttpE2ETests(unittest.TestCase):
    def setUp(self):
        self.state = {"cargo": {}, "events": {}, "events_by_id": {}, "locks": set()}

        @contextmanager
        def fake_transaction():
            yield FakeTx(self.state)

        def fake_get_pool():
            class _Pool:
                @contextmanager
                def connection(self_inner):
                    yield FakeTx(self.state)
            return _Pool()

        self._orig_tx = db.transaction
        self._orig_pool = db.get_pool
        self._orig_init = db.init_pool
        self._orig_close = db.close_pool
        db.transaction = fake_transaction
        db.get_pool = fake_get_pool
        db.init_pool = lambda: None
        db.close_pool = lambda: None
        self.client = TestClient(create_app())

    def tearDown(self):
        db.transaction = self._orig_tx
        db.get_pool = self._orig_pool
        db.init_pool = self._orig_init
        db.close_pool = self._orig_close

    # ---- helpers ----
    def cargo(self, no="C1", start="2026-01-01T00:00:00Z", limit="5.0",
              holder="A"):
        return self.client.post("/api/cargo", json={
            "cargo_no": no, "start_time": start,
            "temp_upper_limit": limit, "initial_holder": holder})

    def measure(self, eid, at, temp, no="C1"):
        return self.client.post("/api/events/measurements", json={
            "event_id": eid, "cargo_no": no,
            "occurred_at": at, "temperature": temp})

    def handover(self, eid, at, frm, to, no="C1"):
        return self.client.post("/api/events/handovers", json={
            "event_id": eid, "cargo_no": no, "occurred_at": at,
            "from_party": frm, "to_party": to})

    def report(self, no="C1"):
        return self.client.get(f"/api/cargo/{no}/violations")

    # ---- tests ----
    def test_full_flow_split_and_status_codes(self):
        self.assertEqual(self.cargo().status_code, 201)
        self.assertEqual(self.measure("m1", "2026-01-01T01:00:00Z", "6.0").status_code, 201)
        self.assertEqual(self.handover("h1", "2026-01-01T03:00:00Z", "A", "B").status_code, 201)
        self.assertEqual(self.measure("m2", "2026-01-01T04:00:00Z", "4.5").status_code, 201)

        rep = self.report().json()
        self.assertEqual(rep["current_holder"], "B")
        self.assertEqual(len(rep["violations"]), 1)
        v = rep["violations"][0]
        self.assertEqual(v["start"], "2026-01-01T01:00:00Z")
        self.assertEqual(v["end"], "2026-01-01T04:00:00Z")
        self.assertEqual(v["segments"], [
            {"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T03:00:00Z", "party": "A"},
            {"start": "2026-01-01T03:00:00Z", "end": "2026-01-01T04:00:00Z", "party": "B"},
        ])

    def test_open_interval_has_null_end(self):
        self.cargo()
        self.measure("m1", "2026-01-01T01:00:00Z", "9.9")
        rep = self.report().json()
        self.assertIsNone(rep["violations"][0]["end"])
        self.assertIsNone(rep["violations"][0]["segments"][0]["end"])
        self.assertEqual(rep["violations"][0]["segments"][0]["party"], "A")

    def test_idempotent_replay_200_then_mismatch_409(self):
        self.cargo()
        self.handover("h1", "2026-01-01T02:00:00Z", "A", "B")
        again = self.handover("h1", "2026-01-01T02:00:00Z", "A", "B")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["deduplicated"])

        bad = self.handover("h1", "2026-01-01T02:00:00Z", "A", "X")
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(bad.json()["code"], "event_id_content_mismatch")
        # 既有结果保持不变
        self.assertEqual(self.report().json()["current_holder"], "B")

    def test_wrong_custodian_rejected_and_state_unchanged(self):
        self.cargo()
        r = self.handover("bad", "2026-01-01T02:00:00Z", "X", "B")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "invalid_custodian")
        # 失败事件未落库，保管方仍为 A
        self.assertEqual(self.report().json()["current_holder"], "A")
        self.assertEqual(self.client.get("/api/events?cargo_no=C1").json(), [])

    def test_same_time_conflict_409(self):
        self.cargo()
        self.assertEqual(self.measure("m1", "2026-01-01T01:00:00Z", "6").status_code, 201)
        r = self.measure("m2", "2026-01-01T01:00:00Z", "7")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "same_time_conflict")

    def test_same_time_measurement_and_handover_allowed(self):
        self.cargo()
        self.measure("m1", "2026-01-01T02:00:00Z", "6")
        r = self.handover("h1", "2026-01-01T02:00:00Z", "A", "B")
        self.assertEqual(r.status_code, 201)
        # 交接时刻同时测温超限：责任自 B 起算
        rep = self.report().json()
        self.assertEqual(rep["violations"][0]["segments"],
                         [{"start": "2026-01-01T02:00:00Z",
                           "end": None, "party": "B"}])

    def test_before_start_422(self):
        self.cargo()
        r = self.measure("m0", "2025-12-31T23:00:00Z", "9")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "event_before_start")

    def test_cargo_not_found_404(self):
        self.assertEqual(self.client.get("/api/cargo/NOPE/violations").status_code, 404)
        r = self.measure("m1", "2026-01-01T01:00:00Z", "6", no="NOPE")
        self.assertEqual(r.status_code, 404)

    def test_naive_datetime_rejected(self):
        r = self.client.post("/api/cargo", json={
            "cargo_no": "C2", "start_time": "2026-01-01T00:00:00",
            "temp_upper_limit": "5", "initial_holder": "A"})
        self.assertEqual(r.status_code, 422)

    def test_duplicate_cargo_409(self):
        self.cargo()
        self.assertEqual(self.cargo().status_code, 409)

    def test_late_events_recompute(self):
        self.cargo()
        self.measure("m1", "2026-01-01T01:00:00Z", "6")
        self.measure("m3", "2026-01-01T03:00:00Z", "7")
        self.handover("h1", "2026-01-01T02:00:00Z", "A", "B")
        # 晚到 01:30 正常测温：原开放区间关闭，之后 03:00 另开
        self.measure("late", "2026-01-01T01:30:00Z", "4")
        rep = self.report().json()
        self.assertEqual(len(rep["violations"]), 2)
        self.assertEqual([v["end"] for v in rep["violations"]],
                         ["2026-01-01T01:30:00Z", None])

    def test_late_handover_breaking_chain_rejected(self):
        self.cargo()
        self.handover("h1", "2026-01-01T02:00:00Z", "A", "B")
        self.handover("h2", "2026-01-01T04:00:00Z", "B", "C")
        # 晚到 03:00 由 A 交出（当时保管方已是 B）-> 整体拒绝
        r = self.handover("hlate", "2026-01-01T03:00:00Z", "A", "X")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.report().json()["current_holder"], "C")

    def test_timezone_normalization(self):
        # 09:00+08:00 == 01:00Z，视为同一时刻冲突
        self.cargo()
        self.measure("m1", "2026-01-01T01:00:00Z", "6")
        r = self.measure("m2", "2026-01-01T09:00:00+08:00", "7")
        self.assertEqual(r.status_code, 409)

    def test_health_and_root(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/").json()["service"],
                         "cold-chain-accountability")


if __name__ == "__main__":
    unittest.main()
