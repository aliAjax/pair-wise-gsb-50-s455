"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfer_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    transfer_no TEXT NOT NULL UNIQUE,
                    round INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    suspected_amount REAL,
                    basis TEXT NOT NULL,
                    clue_detail TEXT NOT NULL DEFAULT '',
                    proposal_note TEXT NOT NULL DEFAULT '',
                    confirm_note TEXT NOT NULL DEFAULT '',
                    reject_note TEXT NOT NULL DEFAULT '',
                    deliver_channel TEXT NOT NULL DEFAULT '',
                    recipient_org TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    supplement_required TEXT NOT NULL DEFAULT '',
                    police_org TEXT NOT NULL DEFAULT '',
                    police_case_no TEXT NOT NULL DEFAULT '',
                    result_note TEXT NOT NULL DEFAULT '',
                    retransfer INTEGER NOT NULL DEFAULT 0,
                    reassess_note TEXT NOT NULL DEFAULT '',
                    reassessed_amount REAL,
                    proposed_by TEXT NOT NULL DEFAULT '',
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    sent_by TEXT NOT NULL DEFAULT '',
                    returned_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_transfer_record ON transfer_ledger(record_id, round);
                CREATE INDEX IF NOT EXISTS idx_transfer_status ON transfer_ledger(status);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def mutate_with_transfer(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        transfer_op: Dict[str, Any],
        transfer_no: str = "",
    ) -> Dict[str, Any]:
        """同一事务内更新案件、写审计、更新移送台账，避免台账与案件状态脱节。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            self._apply_transfer_op(connection, record_id, actor_id, transfer_op, transfer_no, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _apply_transfer_op(connection: sqlite3.Connection, record_id: int, actor_id: str, op: Dict[str, Any], transfer_no: str, now: str) -> None:
        op_type = op["type"]
        if op_type == "propose":
            # 同轮次的建议曾被驳回：复用原台账行（含原占位编号），重新进入待确认
            prior = connection.execute(
                "SELECT * FROM transfer_ledger WHERE record_id=? AND round=? ORDER BY id DESC LIMIT 1",
                (record_id, int(op["round"])),
            ).fetchone()
            if prior is not None:
                if prior["status"] != "rejected":
                    raise Conflict("该轮移送建议已存在，不能重复发起")
                connection.execute(
                    """UPDATE transfer_ledger SET status=?, suspected_amount=?, basis=?, clue_detail=?,
                       proposal_note=?, retransfer=?, proposed_by=?, updated_at=?,
                       reject_note='', confirm_note='' WHERE id=?""",
                    (
                        op["status"],
                        float(op["suspected_amount"]),
                        json.dumps(op["basis"], ensure_ascii=False, sort_keys=True),
                        op["clue_detail"],
                        op["proposal_note"],
                        1 if op.get("retransfer") else 0,
                        actor_id,
                        now,
                        int(prior["id"]),
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO transfer_ledger(
                        record_id, transfer_no, round, status, suspected_amount, basis, clue_detail,
                        proposal_note, retransfer, proposed_by, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id,
                        transfer_no,
                        int(op["round"]),
                        op["status"],
                        float(op["suspected_amount"]),
                        json.dumps(op["basis"], ensure_ascii=False, sort_keys=True),
                        op["clue_detail"],
                        op["proposal_note"],
                        1 if op.get("retransfer") else 0,
                        actor_id,
                        now,
                        now,
                    ),
                )
            return
        ledger = connection.execute(
            "SELECT * FROM transfer_ledger WHERE record_id=? ORDER BY round DESC, id DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if ledger is None:
            raise NotFound("移送台账不存在，无法执行该操作")
        ledger_id = int(ledger["id"])
        if op_type == "confirm":
            status, note_field, actor_field = "confirmed", "confirm_note", "confirmed_by"
        elif op_type == "send":
            status, note_field, actor_field = "sent", None, "sent_by"
        elif op_type == "return":
            status, note_field, actor_field = "returned", "return_reason", "returned_by"
        elif op_type == "reject":
            status, note_field, actor_field = "rejected", "reject_note", "confirmed_by"
        elif op_type == "result":
            status, note_field, actor_field = op["status"], "result_note", "returned_by"
        else:  # reassess：台账状态不变，仅记录核定结果
            status, note_field, actor_field = ledger["status"], "reassess_note", "returned_by"
        sets = ["status=?", "updated_at=?"]
        params: List[Any] = [status, now]
        if actor_field:
            sets.append("%s=?" % actor_field)
            params.append(actor_id)
        if op_type == "confirm":
            if not transfer_no:
                raise Conflict("缺少正式移送编号")
            sets += ["confirm_note=?", "transfer_no=?"]
            params += [op["confirm_note"], transfer_no]
        elif op_type == "reject":
            sets += ["reject_note=?"]
            params += [op["reject_note"]]
        elif op_type == "send":
            sets += ["deliver_channel=?", "recipient_org=?"]
            params += [op["deliver_channel"], op["recipient_org"]]
        elif op_type == "return":
            sets += ["return_reason=?", "supplement_required=?", "police_org=?"]
            params += [op["return_reason"], op["supplement_required"], op.get("police_org", "")]
        elif op_type == "result":
            sets += ["result_note=?", "police_case_no=?"]
            params += [op["result_note"], op.get("police_case_no", "")]
        elif op_type == "reassess":
            sets += ["reassess_note=?", "reassessed_amount=?"]
            params += [op["note"], float(op["reassessed_amount"])]
        params += [ledger_id]
        connection.execute("UPDATE transfer_ledger SET %s WHERE id=?" % ", ".join(sets), params)

    @staticmethod
    def _transfer_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"])
        item["retransfer"] = bool(item["retransfer"])
        return item

    def next_transfer_seq(self, year: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM transfer_ledger WHERE substr(transfer_no,1,4)=?",
                (year,),
            ).fetchone()
            return int(row["total"]) + 1

    def get_transfer_by_no(self, transfer_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfer_ledger WHERE transfer_no=?", (transfer_no,)).fetchone()
        if row is None:
            raise NotFound("移送记录不存在")
        return self._transfer_row(row)

    def list_transfers(self, record_id: int = None, status: str = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM transfer_ledger"
        clauses = []
        params: List[Any] = []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._transfer_row(row) for row in rows]

    def transfer_todos(self) -> List[Dict[str, Any]]:
        """移送待办：待确认、待发出、待移送结果、待补证核定。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, r.reference, r.state AS record_state
                FROM transfer_ledger t
                JOIN records r ON r.id = t.record_id
                WHERE t.status IN ('proposed','confirmed','sent','returned')
                ORDER BY
                    CASE t.status WHEN 'proposed' THEN 0 WHEN 'confirmed' THEN 1 WHEN 'returned' THEN 2 ELSE 3 END,
                    t.id DESC
                """,
            ).fetchall()
        items = []
        for row in rows:
            item = self._transfer_row(row)
            item["reference"] = row["reference"]
            item["record_state"] = row["record_state"]
            items.append(item)
        return items

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
