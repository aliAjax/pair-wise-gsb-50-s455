"""税务稽查案件与复议流程领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "opened"
CRIMINAL_TAX_THRESHOLD = 500000.0
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {
    'investigate': {'inspector'},
    'propose': {'inspector'},
    'review': {'reviewer'},
    'appeal': {'taxpayer_rep'},
    'close': {'reviewer'},
    # 涉刑移送
    'transfer_propose': {'inspector'},
    'transfer_confirm': {'reviewer'},
    'transfer_send': {'reviewer'},
    'transfer_return': {'reviewer'},
    'transfer_reassess': {'inspector'},
    'transfer_result': {'reviewer'},
}
TRANSITIONS = {
    'investigate': {'opened': 'investigating', 'supplementing': 'supplementing'},
    'propose': {'investigating': 'proposed'},
    'review': {'proposed': 'reviewed'},
    'appeal': {'reviewed': 'appealed'},
    # 移送建议可在调查、处理建议、复核完成（含复议后）阶段发起；补证核定后可再次发起
    'transfer_propose': {
        'investigating': 'transfer_pending',
        'proposed': 'transfer_pending',
        'reviewed': 'transfer_pending',
        'appealed': 'transfer_pending',
        'supplementing': 'transfer_pending',
    },
    'transfer_confirm': {'transfer_pending': 'awaiting_dispatch'},
    'transfer_reject': {'transfer_pending': 'investigating'},
    'transfer_send': {'awaiting_dispatch': 'awaiting_transfer'},
    'transfer_return': {'awaiting_transfer': 'supplementing'},
    'transfer_result': {
        'awaiting_transfer': 'transfer_accepted',
        'supplementing': 'transfer_accepted',
    },
    'transfer_reassess': {'supplementing': 'supplementing'},
    'close': {
        'reviewed': 'closed',
        'appealed': 'closed',
        'transfer_accepted': 'closed',
    },
}
# 移送流程未完结时禁止结案，杜绝"普通案件"方式提前归档
TRANSFER_OPEN_STATES = {'transfer_pending', 'awaiting_dispatch', 'awaiting_transfer', 'supplementing'}
LEDGER_STATUSES = {'proposed', 'confirmed', 'sent', 'returned', 'accepted', 'rejected'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    CRIMINAL_TAX_THRESHOLD = CRIMINAL_TAX_THRESHOLD

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        if action == "transfer_reject":
            # 驳回与确认同为负责人审批动作
            return role == "admin" or role in ACTION_ROLES["transfer_confirm"]
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "taxpayer")
        text(p, "tax_period")
        number(p, "declared_tax", 0)
        number(p, "assessed_tax", 0)
        number(p, "penalty_rate", 0, 1)
        integer(p, "evidence_count", 0)
        integer(p, "days_late", 0)
        integer(p, "appeal_deadline_day", 1)
        # 涉刑线索：隐瞒收入、销毁资料（缺省为否，兼容旧数据）
        p["hidden_income"] = boolean(p, "hidden_income", False)
        p["destroyed_records"] = boolean(p, "destroyed_records", False)
        p["criminal_clues"] = optional_text(p, "criminal_clues", "")
        return p

    @staticmethod
    def recalc_amounts(p: Dict[str, Any], assessed_tax: float, days_late: int = None) -> Dict[str, float]:
        declared = float(p["declared_tax"])
        if days_late is None:
            days_late = int(p["days_late"])
        difference = max(0.0, assessed_tax - declared)
        interest = difference * 0.0005 * int(days_late)
        penalty = difference * float(p["penalty_rate"])
        return {
            "assessed_tax": round(float(assessed_tax), 2),
            "days_late": int(days_late),
            "tax_difference": round(difference, 2),
            "interest": round(interest, 2),
            "penalty": round(penalty, 2),
            "total_due": round(difference + interest + penalty, 2),
            "refund_due": round(max(0.0, declared - assessed_tax), 2),
        }

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p.update(self.recalc_amounts(p, float(p["assessed_tax"]), int(p["days_late"])))
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def criminal_eligibility(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """涉刑移送门槛：涉案税额超50万元，或存在隐瞒收入、销毁资料等线索。"""
        difference = float(payload.get("tax_difference", 0.0))
        hidden = bool(payload.get("hidden_income", False))
        destroyed = bool(payload.get("destroyed_records", False))
        reasons = []
        if difference > self.CRIMINAL_TAX_THRESHOLD:
            reasons.append("涉案税额超过50万元")
        if hidden:
            reasons.append("存在隐瞒收入线索")
        if destroyed:
            reasons.append("存在销毁资料线索")
        return bool(reasons), {"tax_difference": round(difference, 2), "threshold": self.CRIMINAL_TAX_THRESHOLD, "hidden_income": hidden, "destroyed_records": destroyed, "reasons": reasons}

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if action == "close" and record["state"] in TRANSFER_OPEN_STATES:
            raise Conflict("涉刑移送尚未有结果，不能提前结案")
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查" if record["state"] == "opened" else "补充调查/补证"
        elif action == "propose":
            if int(p["evidence_count"]) <= 0:
                raise ValidationError("没有证据不能提出处理建议")
            changes["proposal"] = text(data, "proposal")
            changes["proposed_amount"] = float(p["total_due"])
            summary = "已提出补税和处罚建议"
        elif action == "review":
            outcome = choice(data, "outcome", ["accepted", "reduced", "remanded"])
            changes["review_outcome"] = outcome
            changes["review_note"] = text(data, "review_note")
            if outcome == "reduced":
                changes["total_due"] = round(float(p["total_due"]) * float(data.get("reduction_pct", 0.5)), 2)
            summary = "复核完成"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["appeal_deadline_day"]):
                raise ValidationError("复议申请超过期限")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "复议申请已受理"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def apply_transfer_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        """涉刑移送动作。返回 (新状态, payload变更, 摘要, 台账操作)。

        台账操作 op:
          propose / reject / confirm / send / return / result
        """
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        op: Optional[Dict[str, Any]] = None
        summary = ""

        if action == "transfer_propose":
            eligible, basis = self.criminal_eligibility(p)
            if not eligible:
                raise ValidationError("不符合涉刑移送条件：涉案税额未超50万元且无隐瞒收入、销毁资料线索")
            if record["state"] == "supplementing" and not p.get("reassessed_after_return"):
                raise ValidationError("公安退回补证后须先重新核定税额，才能再次移送")
            # 上次建议仅被驳回（未确认、未发出）时沿用原版本号；
            # 一旦确认发出并经退回/不予立案，重新建议即另存新版本
            last_status = p.get("last_transfer_status", "")
            last_round = int(p.get("transfer_round", 0))
            if last_round > 0 and last_status in {"proposed", "rejected"}:
                round_no = last_round
                retransfer = False
                return_reason = ""
            else:
                round_no = last_round + 1
                retransfer = last_round > 0
                return_reason = p.get("return_reason", "") if retransfer else ""
            op = {
                "type": "propose",
                "round": round_no,
                "status": "proposed",
                "basis": basis,
                "suspected_amount": round(float(data.get("suspected_amount", p["total_due"])), 2),
                "clue_detail": optional_text(data, "clue_detail", p.get("criminal_clues", "")),
                "proposal_note": text(data, "proposal_note"),
                "retransfer": retransfer,
                "return_reason": return_reason,
            }
            changes["transfer_round"] = round_no
            changes["last_transfer_status"] = "proposed"
            changes["criminal_flag"] = True
            summary = "第%s次提出涉刑移送建议，待负责人确认" % round_no

        elif action == "transfer_confirm":
            if not boolean(data, "confirmed", False):
                # 驳回：走 transfer_reject 的转移（transfer_pending -> investigating）
                raise ValidationError("驳回请使用transfer_reject并填写驳回意见")
            op = {"type": "confirm", "status": "confirmed", "confirm_note": text(data, "confirm_note")}
            changes["last_transfer_status"] = "confirmed"
            summary = "负责人已确认涉刑移送，移送编号生成后可发出"

        elif action == "transfer_reject":
            op = {"type": "reject", "status": "rejected", "reject_note": text(data, "reject_note")}
            changes["last_transfer_status"] = "rejected"
            summary = "负责人驳回涉刑移送建议"

        elif action == "transfer_send":
            op = {
                "type": "send",
                "status": "sent",
                "deliver_channel": optional_text(data, "deliver_channel", "现场移交"),
                "recipient_org": text(data, "recipient_org"),
            }
            changes["last_transfer_status"] = "sent"
            summary = "涉刑移送已发出，案件停在待移送结果，不得提前结案"

        elif action == "transfer_return":
            op = {
                "type": "return",
                "status": "returned",
                "return_reason": text(data, "return_reason"),
                "supplement_required": text(data, "supplement_required"),
                "police_org": optional_text(data, "police_org", ""),
            }
            changes["return_reason"] = op["return_reason"]
            changes["supplement_required"] = op["supplement_required"]
            changes["reassessed_after_return"] = False
            changes["last_transfer_status"] = "returned"
            summary = "公安退回补证：%s" % op["return_reason"]

        elif action == "transfer_reassess":
            new_assessed = number(data, "assessed_tax", 0)
            days_late = integer(data, "days_late", 0) if "days_late" in data else None
            recalc = self.recalc_amounts(p, new_assessed, days_late)
            changes.update(recalc)
            changes["reassess_note"] = text(data, "reassess_note")
            changes["reassessed_after_return"] = True
            history = list(p.get("reassess_history", []))
            history.append({"round": int(p.get("transfer_round", 1)), **recalc, "note": changes["reassess_note"]})
            changes["reassess_history"] = history
            op = {
                "type": "reassess",
                "reassessed_amount": recalc["tax_difference"],
                "total_due": recalc["total_due"],
                "note": changes["reassess_note"],
            }
            summary = "退回补证后已重新核定税额，差额调整为%s" % recalc["tax_difference"]

        elif action == "transfer_result":
            outcome = choice(data, "outcome", ["accepted", "declined"])
            op = {
                "type": "result",
                "status": "accepted" if outcome == "accepted" else "returned",
                "result_note": text(data, "result_note"),
                "police_case_no": optional_text(data, "police_case_no", ""),
            }
            if outcome == "declined":
                # 公安不予立案：按退回补证处理，需记录原因后重新核定
                op["status"] = "returned"
                op["return_reason"] = text(data, "result_note")
                changes["return_reason"] = op["return_reason"]
                changes["reassessed_after_return"] = False
                changes["last_transfer_status"] = "returned"
                new_state = "supplementing"
                summary = "公安不予立案，退回补证：%s" % op["return_reason"]
            else:
                changes["police_case_no"] = op["police_case_no"]
                changes["last_transfer_status"] = "accepted"
                summary = "公安已受理立案（%s）" % op["police_case_no"]

        else:
            raise ValidationError("未知涉刑移送动作：%s" % action)

        p.update(changes)
        return new_state, p, summary, op
