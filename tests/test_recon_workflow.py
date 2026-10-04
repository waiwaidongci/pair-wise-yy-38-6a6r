"""汛期闸门对账端到端流程测试。"""
import tempfile
import unittest
from pathlib import Path

from src.recon_service import ReconService
from src.repository import Repository
from src.recon_domain import (CANON_CONFIRMED, INBOX_PENDING,
                              INBOX_SUPERSEDED)


class ReconWorkflowTest(unittest.TestCase):
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

    def obs(self, source, value, ft, **kw):
        payload = {"point": "G1", "metric": kw.get("metric", "water_level"),
                   "field_time": ft, "source": source, "value": value}
        payload.update(kw)
        return payload

    def test_device_overrides_manual_but_keeps_anomaly(self):
        self.svc.ingest_observation(
            self.obs("manual", 12.0, "2026-07-04T08:00:00",
                     anomaly_note="上游暴雨"), "zhang", "duty_officer")
        r = self.svc.ingest_observation(
            self.obs("device", 11.5, "2026-07-04T08:00:00"), "dev", "duty_officer")
        self.assertEqual(r["outcome"], "replace")
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.assertEqual(canon["value"], 11.5)
        self.assertEqual(canon["source"], "device")
        # 人工异常原因必须保留
        self.assertIn("上游暴雨", canon["anomaly_note"])
        # 原人工行标记为被取代
        self.assertEqual(self.svc.recon.list_observations(
            "G1", INBOX_SUPERSEDED)[0]["value"], 12.0)

    def test_confirmed_value_is_locked_against_late_data(self):
        self.svc.ingest_observation(
            self.obs("device", 11.5, "2026-07-04T08:00:00"), "dev", "duty_officer")
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.svc.recon.confirm_canonical(canon["id"], "chief")
        r = self.svc.ingest_observation(
            self.obs("device", 99.0, "2026-07-04T08:00:00",
                     anomaly_note="传感器飞点"), "dev", "duty_officer")
        self.assertEqual(r["outcome"], "locked")
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.assertEqual(canon["value"], 11.5)
        self.assertEqual(canon["confirm_state"], CANON_CONFIRMED)
        self.assertIn("传感器飞点", canon["anomaly_note"])

    def test_water_update_invalidates_pending_and_recalculates(self):
        first = self.svc.ingest_observation(
            self.obs("manual", 12.0, "2026-07-04T08:00:00"), "zhang",
            "duty_officer")
        open_id = first["recalc"][0]["command_id"]
        self.assertEqual(self.svc.recon.get_command(open_id)["command"], "open")
        self.svc.ingest_batch({
            "batch_ref": "B1", "point": "G1", "observations": [
                self.obs("device", 8.0, "2026-07-04T09:00:00", seq=2),
                self.obs("device", 13.0, "2026-07-04T07:30:00", seq=1)]
        }, "dev", "duty_officer")
        self.assertEqual(self.svc.recon.get_command(open_id)["status"],
                         "invalidated")
        pending = self.svc.recon.pending_command("G1")
        self.assertEqual(pending["command"], "close")
        self.assertEqual(pending["recalc_of_id"], open_id)
        self.assertIsNotNone(pending["recalc_source"])

    def test_executed_record_kept_and_requalifies_on_closure(self):
        self.svc.ingest_batch({
            "batch_ref": "B1", "point": "G1", "observations": [
                self.obs("device", 8.0, "2026-07-04T09:00:00", seq=1)]
        }, "dev", "duty_officer")
        close_id = self.svc.recon.pending_command("G1")["id"]
        self.svc.execute_command(close_id, "diao", "dispatcher")
        # 已执行记录保留，初始缺回执 pending
        self.assertEqual(self.svc.recon.get_command(close_id)["status"],
                         "executed")
        self.svc.submit_receipt(
            self.obs("manual", 0.0, "2026-07-04T10:20:00",
                     metric="gate_position"), "zhao", "duty_officer")
        self.svc.submit_receipt(
            self.obs("manual", 0.0, "2026-07-04T10:20:00",
                     metric="gate_flow"), "zhao", "duty_officer")
        result = self.svc.submit_closure({
            "point": "G1", "field_time": "2026-07-04T10:25:00",
            "position_value": 0.0, "flow_value": 0.0},
            "zhao", "chief_engineer")
        self.assertEqual(result["qualification"]["qualification"], "qualified")
        self.assertEqual(self.svc.recon.get_command(close_id)["status"],
                         "executed")

    def test_batch_resume_and_idempotent_retry_no_dup_audit(self):
        batch = {"batch_ref": "B1", "point": "G1", "observations": [
            self.obs("device", 8.0, "2026-07-04T09:00:00", seq=2),
            self.obs("device", 13.0, "2026-07-04T07:30:00", seq=1)]}
        first = self.svc.ingest_batch(batch, "dev", "duty_officer")
        self.assertEqual(first["resume_from_seq"], -1)
        self.assertEqual(first["counts"]["accepted"], 2)
        before = len(self.repo.list_audit())
        retry = self.svc.ingest_batch(batch, "dev", "duty_officer")
        after = len(self.repo.list_audit())
        self.assertEqual(retry["counts"]["duplicate"], 2)
        self.assertEqual(retry["counts"]["accepted"], 0)
        # 重试不重复追加审计
        self.assertEqual(after - before, 0)
        # 续传从已确认序列继续
        cont = self.svc.ingest_batch({
            "batch_ref": "B2", "point": "G1", "observations": [
                self.obs("device", 7.0, "2026-07-04T10:00:00", seq=3)]
        }, "dev", "duty_officer")
        self.assertEqual(cont["resume_from_seq"], 2)

    def test_reconciliation_view_exposes_diff_chain_progress(self):
        self.svc.ingest_observation(
            self.obs("manual", 12.0, "2026-07-04T08:00:00"), "zhang",
            "duty_officer")
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.svc.recon.confirm_canonical(canon["id"], "chief")
        self.svc.ingest_observation(
            self.obs("device", 99.0, "2026-07-04T08:00:00"), "dev",
            "duty_officer")
        view = self.svc.reconciliation_view("viewer", "G1")
        self.assertEqual(view["pending_review_count"], 1)
        self.assertTrue(view["diffs"][0]["canonical_locked"])
        self.assertTrue(any(c["recalc_source"] is not None or
                            c["status"] == "pending"
                            for c in view["recalc_chain"]))
        self.assertGreaterEqual(len(view["resume_progress"]), 0)

    def test_pending_review_approve_and_reject(self):
        # 两个同优先级手报冲突 => 后者留待核
        self.svc.ingest_observation(
            self.obs("manual", 12.0, "2026-07-04T08:00:00"), "zhang",
            "duty_officer")
        canon = self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")
        self.svc.recon.confirm_canonical(canon["id"], "chief")
        r = self.svc.ingest_observation(
            self.obs("manual", 12.4, "2026-07-04T08:00:00"), "li",
            "duty_officer")
        self.assertEqual(r["outcome"], "locked")
        pending = self.svc.pending_review("chief_engineer", "G1")
        self.assertEqual(len(pending), 1)
        # 驳回不改变已确认值
        self.svc.resolve_pending(pending[0]["id"], False, "chief",
                                 "chief_engineer")
        self.assertEqual(self.svc.recon.get_canonical(
            "G1", "water_level", "2026-07-04T08:00:00")["value"], 12.0)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
