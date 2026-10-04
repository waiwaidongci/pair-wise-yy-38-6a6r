import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    # ---- 冲突裁决：设备值优先，人工原因保留 ----

    def test_device_priority_and_manual_reason_preserved(self):
        self.service.submit_reading({
            "point": "GATE-01", "source": "manual", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 5.0, "reason": "人工观测水尺异常",
        }, "officer", "duty_officer")
        dev = self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 8.0,
        }, "officer", "duty_officer")
        self.assertEqual(dev["status"], "confirmed")
        readings = self.service.list_readings("viewer", "GATE-01")
        manual = [r for r in readings if r["source"] == "manual"][0]
        self.assertEqual(manual["status"], "held")
        self.assertEqual(manual["reason"], "人工观测水尺异常")
        recon = self.service.list_reconciliation("viewer", "GATE-01")
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0]["device_value"], 8.0)
        self.assertEqual(recon[0]["manual_value"], 5.0)
        self.assertFalse(recon[0]["match"])
        self.assertIn("人工观测水尺异常", recon[0]["reasons"])

    def test_confirmed_value_locked_later_held(self):
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 8.0,
        }, "officer", "duty_officer")
        manual = self.service.submit_reading({
            "point": "GATE-01", "source": "manual", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 5.0, "reason": "水尺有偏差",
        }, "officer", "duty_officer")
        self.assertEqual(manual["status"], "held")
        readings = self.service.list_readings("viewer", "GATE-01")
        dev = [r for r in readings if r["source"] == "device"][0]
        self.assertEqual(dev["status"], "confirmed")
        self.assertEqual(manual["reason"], "水尺有偏差")

    def test_first_manual_wins_second_held(self):
        first = self.service.submit_reading({
            "point": "GATE-01", "source": "manual", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 5.0,
        }, "officer", "duty_officer")
        second = self.service.submit_reading({
            "point": "GATE-01", "source": "manual", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 6.0,
        }, "officer2", "duty_officer")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "held")

    def test_confirm_locks_manual(self):
        manual = self.service.submit_reading({
            "point": "GATE-01", "source": "manual", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "gate_position", "text_value": "到位",
        }, "officer", "duty_officer")
        confirmed = self.service.confirm_reading(manual["id"], "chief", "chief_engineer")
        self.assertEqual(confirmed["status"], "confirmed")
        later = self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "gate_position", "text_value": "closed",
        }, "officer", "duty_officer")
        self.assertEqual(later["status"], "held")

    # ---- 依据更新：未执行指令失效重算 ----

    def test_basis_update_recalculates_unexecuted(self):
        item = self.service.create_item({
            "title": "recalc item", "description": "basis change", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 9.0,
        }, "officer", "duty_officer")
        updated = self.service.get_item(item["id"], "viewer")
        self.assertEqual(updated["quantity"], 9.0)
        self.assertEqual(updated["invalidated"], 1)
        events = self.service.audit("viewer", item["id"])
        recalc = [e for e in events if e["action"] == "recalculated"]
        self.assertEqual(len(recalc), 1)
        self.assertEqual(recalc[0]["detail"]["new_quantity"], 9.0)
        self.assertEqual(recalc[0]["detail"]["trigger"], "reading")

    def test_authorization_clears_invalidated(self):
        item = self.service.create_item({
            "title": "auth clear", "description": "x", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 9.0,
        }, "officer", "duty_officer")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["invalidated"], 1)
        current = item
        for target in STATES[1:3]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "authorized")
        self.assertEqual(current["invalidated"], 0)

    # ---- 已执行记录保留，重新核对关闭资格 ----

    def test_executed_item_closure_recheck_on_basis_change(self):
        item = self.service.create_item({
            "title": "closure recheck", "description": "x", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        current = item
        for target in STATES[1:4]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "executed")
        # 依据变为高水位，关闭资格不满足
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T09:00:00Z",
            "kind": "water_level", "value": 11.0,
        }, "officer", "duty_officer")
        events = self.service.audit("viewer", item["id"])
        recheck = [e for e in events if e["action"] == "closure_recheck"]
        self.assertTrue(len(recheck) >= 1)
        latest = recheck[-1]
        self.assertFalse(latest["detail"]["eligible"])
        # 已执行记录仍保留
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"], "executed")

    def test_close_blocked_when_not_eligible(self):
        item = self.service.create_item({
            "title": "close block", "description": "x", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "water_level", "value": 11.0,
        }, "officer", "duty_officer")
        current = item
        for target in STATES[1:4]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])

    def test_close_allowed_when_eligible(self):
        item = self.service.create_item({
            "title": "close ok", "description": "x", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        self.service.submit_reading({
            "point": "GATE-01", "source": "device", "observed_at": "2026-10-01T08:00:00Z",
            "kind": "gate_position", "text_value": "到位",
        }, "officer", "duty_officer")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], STATES[-1])

    # ---- 回执更新触发重算 ----

    def test_receipt_update_triggers_recalc(self):
        item = self.service.create_item({
            "title": "receipt recalc", "description": "x", "severity": "urgent",
            "quantity": 5, "threshold": 10, "point": "GATE-01",
        }, "creator", "duty_officer")
        self.service.add_record(item["id"], {
            "kind": "receipt", "detail": "闸门操作回执", "status": "closed",
        }, "recorder", "duty_officer")
        updated = self.service.get_item(item["id"], "viewer")
        self.assertEqual(updated["invalidated"], 1)
        events = self.service.audit("viewer", item["id"])
        recalc = [e for e in events if e["action"] == "recalculated"]
        self.assertEqual(len(recalc), 1)
        self.assertEqual(recalc[0]["detail"]["trigger"], "receipt")

    # ---- 补传：可续传，重试不重复追加审计 ----

    def test_backfill_completes_and_idempotent_retry(self):
        readings = [
            {"point": "GATE-01", "observed_at": "2026-10-01T08:00:00Z", "value": 1.0, "external_ref": "BF-1"},
            {"point": "GATE-01", "observed_at": "2026-10-01T09:00:00Z", "value": 2.0, "external_ref": "BF-2"},
            {"point": "GATE-01", "observed_at": "2026-10-01T10:00:00Z", "value": 3.0, "external_ref": "BF-3"},
        ]
        batch = self.service.submit_backfill({"batch_ref": "BATCH-1", "readings": readings},
                                             "officer", "duty_officer")
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["processed"], 3)
        events = self.service.audit("viewer")
        submitted = [e for e in events if e["action"] == "reading_submitted"]
        self.assertEqual(len(submitted), 3)
        # 重试：已确认测点不重复处理，审计不重复追加
        batch2 = self.service.submit_backfill({"batch_ref": "BATCH-1", "readings": readings},
                                              "officer", "duty_officer")
        self.assertEqual(batch2["status"], "completed")
        events2 = self.service.audit("viewer")
        submitted2 = [e for e in events2 if e["action"] == "reading_submitted"]
        self.assertEqual(len(submitted2), 3)

    def test_backfill_failure_resumes_from_confirmed_point(self):
        bad = [
            {"point": "GATE-01", "observed_at": "2026-10-01T08:00:00Z", "value": 1.0, "external_ref": "BF-1"},
            {"observed_at": "2026-10-01T09:00:00Z", "value": 2.0},  # 缺测点，失败
            {"point": "GATE-01", "observed_at": "2026-10-01T10:00:00Z", "value": 3.0, "external_ref": "BF-3"},
        ]
        with self.assertRaises(ValidationError):
            self.service.submit_backfill({"batch_ref": "BATCH-2", "readings": bad},
                                         "officer", "duty_officer")
        batch = self.service.list_backfills("viewer")[0]
        self.assertEqual(batch["status"], "failed")
        self.assertEqual(batch["processed"], 1)
        self.assertEqual(batch["resume_index"], 1)
        # 续传：从已确认测点继续，已处理的不重复追加审计
        fixed = [
            {"point": "GATE-01", "observed_at": "2026-10-01T08:00:00Z", "value": 1.0, "external_ref": "BF-1"},
            {"point": "GATE-01", "observed_at": "2026-10-01T09:00:00Z", "value": 2.0, "external_ref": "BF-2"},
            {"point": "GATE-01", "observed_at": "2026-10-01T10:00:00Z", "value": 3.0, "external_ref": "BF-3"},
        ]
        batch2 = self.service.submit_backfill({"batch_ref": "BATCH-2", "readings": fixed},
                                               "officer", "duty_officer")
        self.assertEqual(batch2["status"], "completed")
        events = self.service.audit("viewer")
        submitted = [e for e in events if e["action"] == "reading_submitted"]
        # 只有 3 笔读数审计（BF-1 不重复）
        self.assertEqual(len(submitted), 3)
        refs = [e["detail"].get("value") for e in submitted]
        self.assertEqual(sorted(refs), [1.0, 2.0, 3.0])

    # ---- 权限 ----

    def test_reading_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_reading({
                "point": "GATE-01", "source": "manual",
                "observed_at": "2026-10-01T08:00:00Z", "value": 1.0,
            }, "attacker", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_reading(1, "attacker", "viewer")


if __name__ == "__main__":
    unittest.main()
