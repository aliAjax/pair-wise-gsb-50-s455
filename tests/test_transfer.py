import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


BIG_CASE = {'taxpayer': 'Big Ltd', 'tax_period': '2025-Q4', 'declared_tax': 100000.0, 'assessed_tax': 900000.0, 'penalty_rate': 0.5, 'evidence_count': 6, 'days_late': 120, 'appeal_deadline_day': 60}
SMALL_CASE = {'taxpayer': 'Small Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
INSPECTOR = Actor("inspector-1", "inspector")
REVIEWER = Actor("reviewer-1", "reviewer")
LEADER = Actor("leader-1", "leader")


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _reviewed(self, reference, data):
        record = self.service.create(INSPECTOR, reference, data)
        record = self.service.act(INSPECTOR, record["id"], record["version"], "investigate", {"plan": "核对账簿"})
        record = self.service.act(INSPECTOR, record["id"], record["version"], "propose", {"proposal": "补税并处罚"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "review", {"outcome": "accepted", "review_note": "证据充分"})
        return record

    def test_big_case_cannot_close_as_normal(self):
        record = self._reviewed("TAX-90001", BIG_CASE)
        with self.assertRaises(Conflict):
            self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "维持处理"})

    def test_transfer_flow_with_return_and_second_version(self):
        record = self._reviewed("TAX-90002", BIG_CASE)
        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "涉案税额超过50万元", "clues": ["隐瞒收入", "销毁资料"]})
        self.assertEqual(record["state"], "transfer_proposed")
        self.assertEqual(record["payload"]["transfer_version"], 1)
        todo = self.service.transfer_todo(LEADER)
        self.assertEqual(len(todo), 1)
        self.assertEqual(todo[0]["todo"], "待负责人确认移送编号")

        record = self.service.act(LEADER, record["id"], record["version"], "confirm_transfer", {})
        self.assertEqual(record["state"], "transfer_confirmed")
        first_no = record["payload"]["transfer_no"]
        self.assertTrue(first_no.startswith("税移字〔"))

        record = self.service.act(REVIEWER, record["id"], record["version"], "send_transfer", {"sent_to": "市公安局经侦支队"})
        self.assertEqual(record["state"], "transfer_pending_result")
        todo = self.service.transfer_todo(REVIEWER)
        self.assertEqual(todo[0]["todo"], "待公安机关移送结果")
        with self.assertRaises(Conflict):
            self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "提前结案"})

        record = self.service.act(REVIEWER, record["id"], record["version"], "resolve_transfer", {"outcome": "returned", "return_reason": "账簿复印件不完整，需补充原始凭证"})
        self.assertEqual(record["state"], "transfer_returned")
        todo = self.service.transfer_todo(INSPECTOR)
        self.assertEqual(todo[0]["todo"], "待退回补证并重新核定税额")
        self.assertEqual(todo[0]["return_reason"], "账簿复印件不完整，需补充原始凭证")

        record = self.service.act(INSPECTOR, record["id"], record["version"], "reassess", {"assessed_tax": 950000, "days_late": 150, "evidence_count": 9, "note": "补充原始凭证后重新核定"})
        self.assertEqual(record["state"], "reviewed")
        self.assertEqual(record["payload"]["tax_difference"], 850000.0)
        self.assertEqual(record["payload"]["interest"], 63750.0)
        self.assertEqual(record["payload"]["total_due"], 1338750.0)

        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "重新核定后仍超过50万元"})
        self.assertEqual(record["payload"]["transfer_version"], 2)
        record = self.service.act(LEADER, record["id"], record["version"], "confirm_transfer", {})
        second_no = record["payload"]["transfer_no"]
        self.assertNotEqual(first_no, second_no)
        record = self.service.act(REVIEWER, record["id"], record["version"], "send_transfer", {"sent_to": "市公安局经侦支队"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "resolve_transfer", {"outcome": "accepted"})
        self.assertEqual(record["state"], "closed")

        transfers = self.service.record_transfers(REVIEWER, record["id"])
        self.assertEqual(len(transfers), 2)
        self.assertEqual(transfers[0]["version"], 2)
        self.assertEqual(transfers[0]["state"], "accepted")
        self.assertEqual(transfers[0]["transfer_no"], second_no)
        self.assertFalse(transfers[0]["return_reason"])
        self.assertEqual(transfers[1]["version"], 1)
        self.assertEqual(transfers[1]["state"], "returned")
        self.assertEqual(transfers[1]["return_reason"], "账簿复印件不完整，需补充原始凭证")

        accepted = self.service.list_transfers(REVIEWER, state="accepted")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["transfer_no"], second_no)
        self.assertEqual(accepted[0]["taxpayer"], "Big Ltd")

        timeline = self.service.timeline(LEADER, record["id"])
        confirm_events = [e for e in timeline if e["action"] == "confirm_transfer"]
        self.assertEqual(confirm_events[-1]["details"]["transfer"]["transfer_no"], second_no)

    def test_small_case_without_clues_cannot_propose_transfer(self):
        record = self._reviewed("TAX-90003", SMALL_CASE)
        with self.assertRaises(ValidationError):
            self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "尝试移送"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "维持处理"})
        self.assertEqual(record["state"], "closed")

    def test_clues_trigger_transfer_requirement(self):
        data = dict(SMALL_CASE)
        data["taxpayer"] = "Clue Ltd"
        data["clues"] = ["隐瞒收入"]
        record = self._reviewed("TAX-90004", data)
        with self.assertRaises(Conflict):
            self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "维持处理"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "发现隐瞒收入线索"})
        self.assertEqual(record["state"], "transfer_proposed")
        self.assertEqual(record["payload"]["clues"], ["隐瞒收入"])

    def test_confirm_role_and_unique_number(self):
        record = self._reviewed("TAX-90005", BIG_CASE)
        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "税额巨大"})
        with self.assertRaises(PermissionDenied):
            self.service.act(REVIEWER, record["id"], record["version"], "confirm_transfer", {})
        record = self.service.act(LEADER, record["id"], record["version"], "confirm_transfer", {"transfer_no": "自定义-001"})
        self.assertEqual(record["payload"]["transfer_no"], "自定义-001")

        other = self._reviewed("TAX-90006", dict(BIG_CASE, taxpayer="Other Ltd"))
        other = self.service.act(REVIEWER, other["id"], other["version"], "propose_transfer", {"reason": "税额巨大"})
        with self.assertRaises(Conflict):
            self.service.act(LEADER, other["id"], other["version"], "confirm_transfer", {"transfer_no": "自定义-001"})
        other = self.service.act(LEADER, other["id"], other["version"], "confirm_transfer", {})
        self.assertNotEqual(other["payload"]["transfer_no"], "自定义-001")

    def test_return_reason_required_and_reassess_role(self):
        record = self._reviewed("TAX-90007", BIG_CASE)
        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "税额巨大"})
        record = self.service.act(LEADER, record["id"], record["version"], "confirm_transfer", {})
        record = self.service.act(REVIEWER, record["id"], record["version"], "send_transfer", {"sent_to": "市公安局"})
        with self.assertRaises(ValidationError):
            self.service.act(REVIEWER, record["id"], record["version"], "resolve_transfer", {"outcome": "returned"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "resolve_transfer", {"outcome": "returned", "return_reason": "证据链不完整"})
        with self.assertRaises(PermissionDenied):
            self.service.act(REVIEWER, record["id"], record["version"], "reassess", {"assessed_tax": 900000, "note": "越权核定"})

    def test_reassess_below_threshold_can_close_normally(self):
        record = self._reviewed("TAX-90008", BIG_CASE)
        record = self.service.act(REVIEWER, record["id"], record["version"], "propose_transfer", {"reason": "税额巨大"})
        record = self.service.act(LEADER, record["id"], record["version"], "confirm_transfer", {})
        record = self.service.act(REVIEWER, record["id"], record["version"], "send_transfer", {"sent_to": "市公安局"})
        record = self.service.act(REVIEWER, record["id"], record["version"], "resolve_transfer", {"outcome": "returned", "return_reason": "核定依据不足"})
        record = self.service.act(INSPECTOR, record["id"], record["version"], "reassess", {"assessed_tax": 400000, "note": "退回后查实税额下降"})
        self.assertEqual(record["payload"]["tax_difference"], 300000.0)
        record = self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "按重新核定结果处理"})
        self.assertEqual(record["state"], "closed")
