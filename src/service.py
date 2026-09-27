"""业务用例编排、权限检查与审计。"""
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


TRANSFER_ACTIONS = {
    'transfer_propose', 'transfer_confirm', 'transfer_reject', 'transfer_send',
    'transfer_return', 'transfer_reassess', 'transfer_result',
}
TRANSFER_NO_PREFIX = "XS"
# 涉刑移送编号按年顺序：XS-YYYY-NNNNNN
TRANSFER_NUMBER_LOCK = threading.Lock()


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action in TRANSFER_ACTIONS:
            return self._act_transfer(actor, record_id, int(expected_version), action, data or {})
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def _generate_transfer_no(self, round_no: int) -> str:
        """负责人确认时生成全局唯一移送编号：XS-年份-序号。

        序号取当年台账条数+1；若唯一约束冲突（并发确认）则顺延重试。
        """
        year = datetime.now(timezone.utc).strftime("%Y")
        with TRANSFER_NUMBER_LOCK:
            seq = self.repository.next_transfer_seq(year)
            for offset in range(50):
                number = "%s-%s-%06d" % (TRANSFER_NO_PREFIX, year, seq + offset)
                try:
                    self.repository.get_transfer_by_no(number)
                except Exception:
                    return number
        raise Conflict("移送编号生成失败，请重试")

    def _act_transfer(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        new_state, new_payload, summary, op = self.rules.apply_transfer_action(record, action, data)
        transfer_no = ""
        if op and op["type"] == "propose":
            # 建议阶段先给占位编号，负责人确认时才生成正式唯一编号
            transfer_no = "PENDING-%s-%s" % (record_id, op["round"])
        if op and op["type"] == "confirm":
            transfer_no = self._generate_transfer_no(int(new_payload.get("transfer_round", 1)))
        details = {"summary": summary, "input": data, "from": record["state"], "to": new_state}
        if transfer_no and op["type"] == "confirm":
            details["transfer_no"] = transfer_no
        try:
            return self.repository.mutate_with_transfer(
                record_id=record_id,
                expected_version=expected_version,
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
                transfer_op=op,
                transfer_no=transfer_no,
            )
        except Conflict as exc:
            if "transfer_no" in str(exc).lower() or "UNIQUE" in str(exc).upper():
                raise Conflict("移送编号冲突，请重新确认") from exc
            raise

    def list_transfers(self, actor: Actor, record_id: int = None, status: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_transfers(record_id=record_id, status=status)

    def get_transfer(self, actor: Actor, transfer_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_transfer_by_no(text({"transfer_no": transfer_no}, "transfer_no"))

    def transfer_todos(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.transfer_todos()
        groups: Dict[str, list] = {"to_confirm": [], "to_dispatch": [], "awaiting_result": [], "to_reassess": []}
        for item in items:
            status = item["status"]
            bucket = {
                "proposed": "to_confirm",
                "confirmed": "to_dispatch",
                "sent": "awaiting_result",
                "returned": "to_reassess",
            }.get(status)
            if bucket:
                groups[bucket].append(item)
        return {"groups": groups, "total": len(items)}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
