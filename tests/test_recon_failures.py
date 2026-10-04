"""对账并发与失败场景：先到生效/后到留待核、权限、重复关闭确认。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.recon_service import ReconService
from src.repository import Repository


class ReconFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "recon.db"))
        self.svc = ReconService(self.repo)
        self.svc.register_point(
            {"point": "G1", "title": "1号闸", "open_threshold": 10},
            "zhang", "duty_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _obs(self, actor, value):
        return {"point": "G1", "metric": "water_level",
                "field_time": "2026-07-04T08:00:00", "source": "manual",
                "value": value}, actor, "duty_officer"

    def test_concurrent_same_point_first_wins(self):
        results = {}
        barrier = threading.Barrier(2)

        # 放大临界区，保证第二个提交确实撞上线程锁
        from src import recon_service as rs
        original = rs.ReconService._apply_observation

        def slow(self, obs, batch_ref, actor):
            time.sleep(0.2)
            return original(self, obs, batch_ref, actor)
        rs.ReconService._apply_observation = slow
        try:
            def submit(who, value):
                try:
                    barrier.wait()
                    self.svc.ingest_observation(*self._obs(who, value))
                    results[who] = "accepted"
                except ConflictError:
                    results[who] = "held"
                except Exception as exc:  # pragma: no cover
                    results[who] = "error:" + type(exc).__name__

            t1 = threading.Thread(target=submit, args=("zhang", 12.0))
            t2 = threading.Thread(target=submit, args=("li", 13.0))
            t1.start()
            time.sleep(0.05)
            t2.start()
            t1.join()
            t2.join()
        finally:
            rs.ReconService._apply_observation = original

        self.assertEqual(sorted(results.values()), ["accepted", "held"])
        pending = self.svc.pending_review("chief_engineer", "G1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending")
        winner = [w for w, v in results.items() if v == "accepted"][0]
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.assertEqual(canon["value"], 12.0 if winner == "zhang" else 13.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_viewer_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self.svc.ingest_observation(
                {"point": "G1", "metric": "water_level",
                 "field_time": "2026-07-04T08:00:00", "source": "manual",
                 "value": 12.0}, "x", "viewer")

    def test_invalid_field_time_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.ingest_observation(
                {"point": "G1", "metric": "water_level",
                 "field_time": "not-a-time", "source": "manual",
                 "value": 12.0}, "zhang", "duty_officer")

    def test_unknown_point_rejected(self):
        from src.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            self.svc.ingest_observation(
                {"point": "NOPE", "metric": "water_level",
                 "field_time": "2026-07-04T08:00:00", "source": "device",
                 "value": 12.0}, "dev", "duty_officer")

    def test_execute_invalidated_command_conflicts(self):
        r = self.svc.ingest_observation(
            {"point": "G1", "metric": "water_level",
             "field_time": "2026-07-04T08:00:00", "source": "manual",
             "value": 12.0}, "zhang", "duty_officer")
        open_id = r["recalc"][0]["command_id"]
        self.svc.ingest_observation(
            {"point": "G1", "metric": "water_level",
             "field_time": "2026-07-04T09:00:00", "source": "device",
             "value": 8.0}, "dev", "duty_officer")
        with self.assertRaises(ConflictError):
            self.svc.execute_command(open_id, "diao", "dispatcher")

    def test_duplicate_closure_conflicts(self):
        self.svc.ingest_observation(
            {"point": "G1", "metric": "water_level",
             "field_time": "2026-07-04T09:00:00", "source": "device",
             "value": 8.0}, "dev", "duty_officer")
        close_id = self.svc.recon.pending_command("G1")["id"]
        self.svc.execute_command(close_id, "diao", "dispatcher")
        for metric, value in (("gate_position", 0.0), ("gate_flow", 0.0)):
            self.svc.submit_receipt(
                {"point": "G1", "metric": metric,
                 "field_time": "2026-07-04T10:20:00", "source": "manual",
                 "value": value}, "zhao", "duty_officer")
        closure = {"point": "G1", "field_time": "2026-07-04T10:25:00",
                   "position_value": 0.0, "flow_value": 0.0}
        self.svc.submit_closure(closure, "zhao", "chief_engineer")
        with self.assertRaises(ConflictError):
            self.svc.submit_closure(closure, "zhao", "chief_engineer")


if __name__ == "__main__":
    unittest.main()
