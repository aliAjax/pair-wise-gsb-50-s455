import re
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


# 补税差额 70 万，超过 50 万门槛
BIG_CASE = {'taxpayer': 'Hidden Ltd', 'tax_period': '2025-Q4', 'declared_tax': 300000.0, 'assessed_tax': 1000000.0,
            'penalty_rate': 0.3, 'evidence_count': 5, 'days_late': 120, 'appeal_deadline_day': 60,
            'hidden_income': True, 'criminal_clues': '私人账户收款'}
# 补税差额 10 万，且无线索：不符合移送条件
SMALL_CASE = dict(BIG_CASE, taxpayer='Clean Ltd', declared_tax=900000.0, assessed_tax=1000000.0,
                  hidden_income=False, destroyed_records=False, criminal_clues='')
# 差额不大，但有销毁资料线索：仍符合移送条件
CLUE_CASE = dict(BIG_CASE, taxpayer='Burner Ltd', declared_tax=950000.0, assessed_tax=1000000.0,
                 hidden_income=False, destroyed_records=True)

INSPECTOR = Actor("insp", "inspector")
REVIEWER = Actor("boss", "reviewer")


def run(service, record, action, actor, data):
    return service.act(actor, record["id"], record["version"], action, data)


class TransferWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_investigating(self, data=BIG_CASE, ref="TAX-30001"):
        record = self.service.create(INSPECTOR, ref, data)
        return run(self.service, record, "investigate", INSPECTOR, {"plan": "核对账簿流水"})

    def test_full_transfer_flow_with_unique_number_and_lock(self):
        record = self._to_investigating()
        # 发起建议
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "涉嫌逃税，建议移送"})
        self.assertEqual(record["state"], "transfer_pending")
        # 待确认期间不能结案
        with self.assertRaises(Conflict):
            run(self.service, record, "close", REVIEWER, {"final_decision": "强行结案"})
        # 建议阶段编号为占位
        ledger = self.service.list_transfers(REVIEWER, record_id=record["id"])
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger[0]["transfer_no"].startswith("PENDING-"))
        # 检查员无权确认
        with self.assertRaises(PermissionDenied):
            run(self.service, record, "transfer_confirm", INSPECTOR, {"confirmed": True, "confirm_note": "同意"})
        # 负责人确认 -> 唯一编号 XS-YYYY-NNNNNN
        record = run(self.service, record, "transfer_confirm", REVIEWER, {"confirmed": True, "confirm_note": "同意移送"})
        self.assertEqual(record["state"], "awaiting_dispatch")
        ledger = self.service.list_transfers(REVIEWER, record_id=record["id"])
        self.assertRegex(ledger[0]["transfer_no"], r"^XS-\d{4}-\d{6}$")
        transfer_no = ledger[0]["transfer_no"]
        # 发出 -> 待移送结果，仍不能结案
        record = run(self.service, record, "transfer_send", REVIEWER, {"recipient_org": "市公安局经侦支队"})
        self.assertEqual(record["state"], "awaiting_transfer")
        with self.assertRaises(Conflict):
            run(self.service, record, "close", REVIEWER, {"final_decision": "提前结案"})
        # 待办中有待移送结果
        todos = self.service.transfer_todos(REVIEWER)
        awaiting = {t["transfer_no"] for t in todos["groups"]["awaiting_result"]}
        self.assertIn(transfer_no, awaiting)
        # 公安受理后才能结案
        record = run(self.service, record, "transfer_result", REVIEWER,
                     {"outcome": "accepted", "police_case_no": "A2026-0001", "result_note": "立案侦查"})
        self.assertEqual(record["state"], "transfer_accepted")
        record = run(self.service, record, "close", REVIEWER, {"final_decision": "移送后结案"})
        self.assertEqual(record["state"], "closed")

    def test_eligibility_threshold_and_clues(self):
        rules = self.service.rules
        big = self.service.create(INSPECTOR, "TAX-31001", dict(BIG_CASE, tax_period="2025-Q1"))["payload"]
        small = self.service.create(INSPECTOR, "TAX-31002", dict(SMALL_CASE, tax_period="2025-Q2"))["payload"]
        clue = self.service.create(INSPECTOR, "TAX-31003", dict(CLUE_CASE, tax_period="2025-Q3"))["payload"]
        ok, basis = rules.criminal_eligibility(big)
        self.assertTrue(ok)
        self.assertIn("涉案税额超过50万元", basis["reasons"])
        ok, basis = rules.criminal_eligibility(small)
        self.assertFalse(ok)
        self.assertEqual(basis["reasons"], [])
        ok, basis = rules.criminal_eligibility(clue)
        self.assertTrue(ok)
        self.assertIn("存在销毁资料线索", basis["reasons"])
        # 不满足条件直接发起移送建议被拒
        record = self._to_investigating(SMALL_CASE, "TAX-31010")
        with self.assertRaises(ValidationError):
            run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "硬要移送"})

    def test_return_supplement_reassess_and_retransfer_version(self):
        record = self._to_investigating(ref="TAX-32001")
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "建议移送"})
        record = run(self.service, record, "transfer_confirm", REVIEWER, {"confirmed": True, "confirm_note": "同意"})
        first_no = self.service.list_transfers(REVIEWER, record_id=record["id"])[0]["transfer_no"]
        record = run(self.service, record, "transfer_send", REVIEWER, {"recipient_org": "市公安局"})
        # 公安退回补证：记录原因
        record = run(self.service, record, "transfer_return", REVIEWER,
                     {"return_reason": "资金流水不全", "supplement_required": "补充银行流水", "police_org": "经侦支队"})
        self.assertEqual(record["state"], "supplementing")
        self.assertEqual(record["payload"]["return_reason"], "资金流水不全")
        # 未重新核定不能再次移送
        with self.assertRaises(ValidationError):
            run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "再次移送"})
        # 重新核定税额：差额从 70 万变为 120 万
        record = run(self.service, record, "transfer_reassess", INSPECTOR,
                     {"assessed_tax": 1500000.0, "reassess_note": "按流水重新核定"})
        self.assertEqual(record["payload"]["assessed_tax"], 1500000.0)
        self.assertEqual(record["payload"]["tax_difference"], 1200000.0)
        self.assertTrue(record["payload"]["reassessed_after_return"])
        # 再次移送：新版本、新编号，旧版本仍在台账
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "补证后再次移送"})
        record = run(self.service, record, "transfer_confirm", REVIEWER, {"confirmed": True, "confirm_note": "同意再次移送"})
        ledger = self.service.list_transfers(REVIEWER, record_id=record["id"])
        self.assertEqual(len(ledger), 2)
        rounds = sorted(t["round"] for t in ledger)
        self.assertEqual(rounds, [1, 2])
        numbers = {t["transfer_no"] for t in ledger}
        self.assertEqual(len(numbers), 2)
        old = next(t for t in ledger if t["round"] == 1)
        self.assertTrue(old["transfer_no"].startswith("XS-"))
        self.assertEqual(old["status"], "returned")
        self.assertEqual(old["return_reason"], "资金流水不全")
        new = next(t for t in ledger if t["round"] == 2)
        self.assertTrue(new["retransfer"])
        self.assertNotEqual(old["transfer_no"], new["transfer_no"])
        self.assertTrue(re.match(r"^XS-\d{4}-\d{6}$", new["transfer_no"]))

    def test_reject_and_police_decline(self):
        # 负责人驳回建议
        record = self._to_investigating(ref="TAX-33001")
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "建议移送"})
        record = run(self.service, record, "transfer_reject", REVIEWER, {"reject_note": "证据不足，继续调查"})
        self.assertEqual(record["state"], "investigating")
        self.assertEqual(self.service.list_transfers(REVIEWER, record_id=record["id"])[0]["status"], "rejected")
        # 再次建议可正常走通
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "补充证据后再建议"})
        self.assertEqual(record["state"], "transfer_pending")
        self.assertEqual(record["payload"]["transfer_round"], 1)
        record = run(self.service, record, "transfer_confirm", REVIEWER, {"confirmed": True, "confirm_note": "同意"})
        ledger = self.service.list_transfers(REVIEWER, record_id=record["id"])
        self.assertEqual(len(ledger), 1)  # 驳回后重提不另存版本
        self.assertEqual(ledger[0]["round"], 1)
        self.assertTrue(ledger[0]["transfer_no"].startswith("XS-"))
        self.assertEqual(ledger[0]["reject_note"], "")

        # 公安不予立案 -> 视同退回，记录原因，要求重新核定
        record = run(self.service, record, "transfer_send", REVIEWER, {"recipient_org": "市公安局"})
        record = run(self.service, record, "transfer_result", REVIEWER,
                     {"outcome": "declined", "result_note": "不达刑事立案标准"})
        self.assertEqual(record["state"], "supplementing")
        self.assertEqual(record["payload"]["return_reason"], "不达刑事立案标准")

    def test_todos_and_status_queries(self):
        record = self._to_investigating(ref="TAX-34001")
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "建议移送"})
        todos = self.service.transfer_todos(REVIEWER)
        self.assertEqual(len(todos["groups"]["to_confirm"]), 1)
        record = run(self.service, record, "transfer_confirm", REVIEWER, {"confirmed": True, "confirm_note": "同意"})
        transfer_no = self.service.list_transfers(REVIEWER, record_id=record["id"])[0]["transfer_no"]
        # 按编号查询
        found = self.service.get_transfer(REVIEWER, transfer_no)
        self.assertEqual(found["record_id"], record["id"])
        # 按状态过滤
        confirmed = self.service.list_transfers(REVIEWER, status="confirmed")
        self.assertEqual([t["transfer_no"] for t in confirmed], [transfer_no])
        # 审计时间线包含移送动作与编号
        timeline = self.service.timeline(INSPECTOR, record["id"])
        actions = [e["action"] for e in timeline]
        self.assertIn("transfer_propose", actions)
        confirm_event = next(e for e in timeline if e["action"] == "transfer_confirm")
        self.assertEqual(confirm_event["details"]["transfer_no"], transfer_no)

    def test_transfer_from_reviewed_blocks_normal_close(self):
        # 复核完成、税额超标：不能再以普通案件结案，必须先走移送
        record = self.service.create(INSPECTOR, "TAX-35001", BIG_CASE)
        record = run(self.service, record, "investigate", INSPECTOR, {"plan": "调查"})
        record = run(self.service, record, "propose", INSPECTOR, {"proposal": "补税并处罚"})
        record = run(self.service, record, "review", REVIEWER, {"outcome": "accepted", "review_note": "证据充分"})
        record = run(self.service, record, "transfer_propose", INSPECTOR, {"proposal_note": "复核阶段移送"})
        with self.assertRaises(Conflict):
            run(self.service, record, "close", REVIEWER, {"final_decision": "普通结案"})


if __name__ == "__main__":
    unittest.main()
