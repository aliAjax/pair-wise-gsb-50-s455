"""税务稽查案件与复议流程领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {
    'investigate': {'inspector'},
    'propose': {'inspector'},
    'review': {'reviewer'},
    'appeal': {'taxpayer_rep'},
    'close': {'reviewer'},
    'propose_transfer': {'reviewer'},
    'confirm_transfer': {'leader'},
    'send_transfer': {'reviewer'},
    'resolve_transfer': {'reviewer'},
    'reassess': {'inspector'},
}
DYNAMIC_TARGET = "?"
TRANSITIONS = {
    'investigate': {'opened': 'investigating'},
    'propose': {'investigating': 'proposed'},
    'review': {'proposed': 'reviewed'},
    'appeal': {'reviewed': 'appealed'},
    'close': {'reviewed': 'closed', 'appealed': 'closed'},
    'propose_transfer': {'reviewed': 'transfer_proposed'},
    'confirm_transfer': {'transfer_proposed': 'transfer_confirmed'},
    'send_transfer': {'transfer_confirmed': 'transfer_pending_result'},
    'resolve_transfer': {'transfer_pending_result': DYNAMIC_TARGET},
    'reassess': {'transfer_returned': 'reviewed'},
}
# 涉案税额超过该阈值，或存在隐瞒收入、销毁资料等线索时，必须走涉刑移送，不能按普通案件结案
TRANSFER_TAX_THRESHOLD = 500000.0


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
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
        p["clues"] = text_list(p, "clues", 0)
        return p

    @staticmethod
    def _recalculate(p: Dict[str, Any]) -> Dict[str, Any]:
        difference = max(0.0, float(p["assessed_tax"]) - float(p["declared_tax"]))
        interest = difference * 0.0005 * int(p["days_late"])
        penalty = difference * float(p["penalty_rate"])
        p["tax_difference"] = round(difference, 2)
        p["interest"] = round(interest, 2)
        p["penalty"] = round(penalty, 2)
        p["total_due"] = round(difference + interest + penalty, 2)
        p["refund_due"] = round(max(0.0, float(p["declared_tax"]) - float(p["assessed_tax"])), 2)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._recalculate(self.validate_create(payload))

    def transfer_required(self, payload: Dict[str, Any]) -> bool:
        clues = payload.get("clues") or []
        return float(payload.get("tax_difference") or 0) > TRANSFER_TAX_THRESHOLD or bool(clues)

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
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
            summary = "进入稽查调查"
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
            if data.get("clues") is not None:
                changes["clues"] = text_list(data, "clues", 0)
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
            if self.transfer_required(p) and not p.get("transfer_accepted"):
                raise Conflict("案件涉嫌犯罪，须先完成涉刑移送，不能按普通案件结案")
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        elif action == "propose_transfer":
            data_clues = text_list(data, "clues", 0)
            existing = [c for c in (p.get("clues") or []) if isinstance(c, str) and c.strip()]
            clues = existing + [c for c in data_clues if c not in existing]
            if float(p.get("tax_difference") or 0) <= TRANSFER_TAX_THRESHOLD and not clues:
                raise ValidationError("涉案税额未超过50万元且无隐瞒收入、销毁资料等线索，不符合涉刑移送条件")
            changes["clues"] = clues
            changes["transfer_reason"] = text(data, "reason")
            summary = "已提出涉刑移送建议，待负责人确认移送编号"
        elif action == "confirm_transfer":
            changes["transfer_confirmed"] = True
            summary = "负责人已确认移送编号"
        elif action == "send_transfer":
            changes["transfer_sent_to"] = text(data, "sent_to")
            summary = "移送文书已发出，案件等待公安机关移送结果"
        elif action == "resolve_transfer":
            outcome = choice(data, "outcome", ["accepted", "returned"])
            changes["transfer_outcome"] = outcome
            if outcome == "accepted":
                new_state = "closed"
                changes["transfer_accepted"] = True
                changes["final_decision"] = optional_text(data, "note") or "涉刑移送公安机关已受理，案件移送结案"
                summary = "公安机关已受理，案件移送结案"
            else:
                new_state = "transfer_returned"
                changes["last_return_reason"] = text(data, "return_reason")
                summary = "公安机关退回补证，待重新核定税额"
        elif action == "reassess":
            changes["assessed_tax"] = number(data, "assessed_tax", 0)
            if data.get("days_late") is not None:
                changes["days_late"] = integer(data, "days_late", 0)
            if data.get("penalty_rate") is not None:
                changes["penalty_rate"] = number(data, "penalty_rate", 0, 1)
            if data.get("evidence_count") is not None:
                changes["evidence_count"] = integer(data, "evidence_count", 0)
            if data.get("clues") is not None:
                changes["clues"] = text_list(data, "clues", 0)
            changes["reassess_note"] = text(data, "note")
            summary = "退回补证后已重新核定税额"
        p.update(changes)
        if action == "reassess":
            self._recalculate(p)
        return new_state, p, summary or ("已执行%s" % action)

    def transfer_effect(self, action: str, payload: Dict[str, Any], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """生成移送台账写入指令，与移送无关的动作返回None。需在apply_action之后调用。"""
        data = data or {}
        if action == "propose_transfer":
            return {"op": "create", "clues": list(payload.get("clues") or []), "reason": payload.get("transfer_reason", "")}
        if action == "confirm_transfer":
            return {"op": "confirm", "transfer_no": optional_text(data, "transfer_no") or None}
        if action == "send_transfer":
            return {"op": "send", "sent_to": payload.get("transfer_sent_to", ""), "note": optional_text(data, "sent_note")}
        if action == "resolve_transfer":
            return {"op": "resolve", "outcome": payload.get("transfer_outcome", ""), "return_reason": optional_text(data, "return_reason")}
        return None
