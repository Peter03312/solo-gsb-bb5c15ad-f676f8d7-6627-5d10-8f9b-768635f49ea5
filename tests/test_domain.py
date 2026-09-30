"""核心领域逻辑测试：python -m unittest discover -s tests -v"""
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.domain import (
    Cargo, Event, DomainError,
    MEASUREMENT, HANDOVER,
    ensure_not_before_start, ensure_no_same_time_conflicts,
    ensure_replay_consistent, events_semantically_equal,
    reconstruct_chain, validate_and_replay,
)

TZ = timezone.utc
def dt(h, m=0):
    return datetime(2026, 1, 1, h, m, tzinfo=TZ)


def m(eid, when, temp):
    return Event(eid, "C1", MEASUREMENT, when, temperature=Decimal(str(temp)))


def h(eid, when, frm, to):
    return Event(eid, "C1", HANDOVER, when, from_party=frm, to_party=to)


CARGO = Cargo("C1", dt(0), Decimal("5.0"), "A")


class ReconstructionTests(unittest.TestCase):
    def test_open_and_close(self):
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), m("m2", dt(3), 4)])
        self.assertEqual(len(r["violations"]), 1)
        v = r["violations"][0]
        self.assertEqual((v["start"], v["end"]), (dt(1), dt(3)))
        self.assertEqual(v["segments"], [{"start": dt(1), "end": dt(3), "party": "A"}])

    def test_still_open_is_marked_open(self):
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6)])
        v = r["violations"][0]
        self.assertIsNone(v["end"])
        self.assertIsNone(v["segments"][0]["end"])
        self.assertEqual(v["segments"][0]["party"], "A")

    def test_equal_to_limit_neither_opens_nor_closes(self):
        # 5.0 == 上限：不开启；已开启时 5.0 关闭（<= 上限即关闭）。
        r1 = reconstruct_chain(CARGO, [m("m1", dt(1), 5)])
        self.assertEqual(r1["violations"], [])
        r2 = reconstruct_chain(CARGO, [m("m1", dt(1), 6), m("m2", dt(2), 5)])
        self.assertEqual((r2["violations"][0]["start"], r2["violations"][0]["end"]),
                         (dt(1), dt(2)))

    def test_split_at_handover_receiver_owns_handover_instant(self):
        # 01:00 越温开始，02:00 A->B 交接，03:00 降温关闭
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), h("h1", dt(2), "A", "B"),
                                      m("m2", dt(3), 4)])
        v = r["violations"][0]
        self.assertEqual(v["segments"], [
            {"start": dt(1), "end": dt(2), "party": "A"},
            {"start": dt(2), "end": dt(3), "party": "B"},
        ])
        # 保管链
        self.assertEqual([(s, e, p) for s, e, p in r["custody"]],
                         [(dt(0), dt(2), "A"), (dt(2), None, "B")])

    def test_open_interval_split_with_open_tail(self):
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), h("h1", dt(2), "A", "B")])
        v = r["violations"][0]
        self.assertIsNone(v["end"])
        self.assertEqual(v["segments"], [
            {"start": dt(1), "end": dt(2), "party": "A"},
            {"start": dt(2), "end": None, "party": "B"},
        ])

    def test_violation_start_exactly_at_handover_belongs_to_receiver(self):
        # 交接时刻 02:00 测温越温：[02:00, ..) 归接收方 B
        r = reconstruct_chain(CARGO, [h("h1", dt(2), "A", "B"), m("m1", dt(2), 6),
                                      m("m2", dt(3), 4)])
        v = r["violations"][0]
        self.assertEqual(v["segments"], [
            {"start": dt(2), "end": dt(3), "party": "B"},
        ])

    def test_violation_end_exactly_at_handover_has_no_receiver_segment(self):
        # 交接时刻 02:00 测温恢复：区间在 02:00 关闭，B 不承担
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), h("h1", dt(2), "A", "B"),
                                      m("m2", dt(2), 4)])
        v = r["violations"][0]
        self.assertEqual(v["segments"], [
            {"start": dt(1), "end": dt(2), "party": "A"},
        ])

    def test_multiple_handovers_three_parties(self):
        events = [
            m("m1", dt(1), 6),
            h("h1", dt(2), "A", "B"),
            h("h2", dt(3), "B", "C"),
            m("m2", dt(4), 4),
        ]
        r = reconstruct_chain(CARGO, events)
        self.assertEqual(r["violations"][0]["segments"], [
            {"start": dt(1), "end": dt(2), "party": "A"},
            {"start": dt(2), "end": dt(3), "party": "B"},
            {"start": dt(3), "end": dt(4), "party": "C"},
        ])

    def test_wrong_custodian_rejected(self):
        with self.assertRaises(DomainError) as cm:
            reconstruct_chain(CARGO, [h("h1", dt(2), "X", "B")])
        self.assertEqual(cm.exception.code, "invalid_custodian")

    def test_late_handover_can_break_chain(self):
        # 已有 A->B、B->C 的合法交接；晚到一笔 A->X（发生在两笔之间），
        # 它使既有 B->C 的交出方不再等于当时保管方 -> 整体拒绝。
        events = [
            h("h1", dt(2), "A", "B"),
            h("h3", dt(4), "B", "C"),
            h("h_late", dt(3), "A", "X"),  # 03:00 时保管方已是 B
        ]
        with self.assertRaises(DomainError) as cm:
            validate_and_replay(CARGO, events)
        self.assertEqual(cm.exception.code, "invalid_custodian")

    def test_late_normal_measurement_recomputes_responsibility(self):
        # 先有 01:00 越温、03:00 仍越温（区间开放，全归 A）；
        # 晚到 02:00 一笔正常测温会把区间切成 [01,02) 一段已闭合，
        # [03:00,..) 重新开启一段新区间。
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), m("m3", dt(3), 7),
                                      m("m_late", dt(2), 4)])
        self.assertEqual(len(r["violations"]), 2)
        self.assertEqual(r["violations"][0]["start"], dt(1))
        self.assertEqual(r["violations"][0]["end"], dt(2))
        self.assertEqual(r["violations"][1]["start"], dt(3))
        self.assertIsNone(r["violations"][1]["end"])

    def test_high_then_high_keeps_single_interval(self):
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 6), m("m2", dt(2), 7)])
        self.assertEqual(len(r["violations"]), 1)
        self.assertIsNone(r["violations"][0]["end"])

    def test_no_violation_when_all_normal(self):
        r = reconstruct_chain(CARGO, [m("m1", dt(1), 2), m("m2", dt(2), 3)])
        self.assertEqual(r["violations"], [])

    def test_handover_exactly_at_start_skips_zero_length_custody(self):
        r = reconstruct_chain(CARGO, [h("h0", dt(0), "A", "B")])
        self.assertEqual([(s, e, p) for s, e, p in r["custody"]],
                         [(dt(0), None, "B")])
        # 交出方仍必须等于起始保管方
        with self.assertRaises(DomainError):
            reconstruct_chain(CARGO, [h("h0", dt(0), "X", "B")])


class ValidationTests(unittest.TestCase):
    def test_event_before_start_rejected(self):
        with self.assertRaises(DomainError) as cm:
            ensure_not_before_start([m("m0", dt(0) - timedelta(minutes=1), 6)], CARGO)
        self.assertEqual(cm.exception.code, "event_before_start")

    def test_event_at_start_allowed(self):
        ensure_not_before_start([m("m0", dt(0), 6)], CARGO)

    def test_same_time_two_measurements_conflict(self):
        with self.assertRaises(DomainError) as cm:
            ensure_no_same_time_conflicts([m("m1", dt(1), 6), m("m2", dt(1), 4)])
        self.assertEqual(cm.exception.code, "same_time_conflict")

    def test_same_time_two_handovers_conflict(self):
        with self.assertRaises(DomainError) as cm:
            ensure_no_same_time_conflicts([h("h1", dt(1), "A", "B"),
                                           h("h2", dt(1), "A", "C")])
        self.assertEqual(cm.exception.code, "same_time_conflict")

    def test_same_time_measurement_and_handover_allowed(self):
        ensure_no_same_time_conflicts([m("m1", dt(1), 6), h("h1", dt(1), "A", "B")])

    def test_idempotent_same_content(self):
        old = m("e1", dt(1), 6)
        self.assertIs(ensure_replay_consistent([old], m("e1", dt(1), 6)), old)

    def test_same_id_different_content_rejected(self):
        old = m("e1", dt(1), 6)
        with self.assertRaises(DomainError) as cm:
            ensure_replay_consistent([old], m("e1", dt(1), 7))
        self.assertEqual(cm.exception.code, "event_id_content_mismatch")

    def test_same_id_different_kind_rejected(self):
        old = m("e1", dt(1), 6)
        with self.assertRaises(DomainError):
            ensure_replay_consistent([old], h("e1", dt(1), "A", "B"))

    def test_decimal_equivalence(self):
        self.assertTrue(events_semantically_equal(m("e", dt(1), 6.0),
                                                  m("e", dt(1), "6.00")))

    def test_timezone_aware_comparison(self):
        # 同一绝对时刻、不同时区表示，不构成冲突
        e1 = Event("a", "C1", MEASUREMENT, datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=8))),
                   temperature=Decimal("6"))
        e2 = Event("b", "C1", MEASUREMENT, dt(1), temperature=Decimal("4"))
        with self.assertRaises(DomainError):
            ensure_no_same_time_conflicts([e1, e2])


if __name__ == "__main__":
    unittest.main()
