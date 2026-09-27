"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


TRANSFER_TODO_LABELS = {
    "proposed": "待负责人确认移送编号",
    "confirmed": "待发出移送文书",
    "sent": "待公安机关移送结果",
    "returned": "待退回补证并重新核定税额",
}


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
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    transfer_no TEXT UNIQUE,
                    state TEXT NOT NULL,
                    clues TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    sent_to TEXT,
                    sent_note TEXT,
                    sent_by TEXT,
                    sent_at TEXT,
                    outcome TEXT,
                    return_reason TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfer_seq (
                    year INTEGER PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_record_version ON transfers(record_id, version);
                CREATE INDEX IF NOT EXISTS idx_transfers_state ON transfers(state);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _transfer_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["clues"] = json.loads(item["clues"])
        payload = json.loads(item.pop("record_payload"))
        item["taxpayer"] = payload.get("taxpayer", "")
        item["tax_difference"] = payload.get("tax_difference")
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], transfer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            if transfer:
                effect = self._apply_transfer(connection, record_id, transfer, actor_id, now)
                if effect["payload"]:
                    payload = dict(payload)
                    payload.update(effect["payload"])
                if effect["audit"]:
                    details = dict(details)
                    details["transfer"] = effect["audit"]
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

    @staticmethod
    def _latest_transfer(connection: sqlite3.Connection, record_id: int, state: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM transfers WHERE record_id=? AND state=? ORDER BY version DESC LIMIT 1",
            (record_id, state),
        ).fetchone()
        if row is None:
            raise Conflict("移送台账状态与案件状态不一致")
        return row

    @staticmethod
    def _next_transfer_no(connection: sqlite3.Connection, now: str) -> str:
        year = int(now[:4])
        connection.execute("INSERT OR IGNORE INTO transfer_seq(year,value) VALUES(?,0)", (year,))
        connection.execute("UPDATE transfer_seq SET value=value+1 WHERE year=?", (year,))
        seq = connection.execute("SELECT value FROM transfer_seq WHERE year=?", (year,)).fetchone()["value"]
        return "税移字〔%d〕%04d号" % (year, int(seq))

    def _apply_transfer(self, connection: sqlite3.Connection, record_id: int, transfer: Dict[str, Any], actor_id: str, now: str) -> Dict[str, Any]:
        """在案件变更事务内同步移送台账，返回需回写案件payload和审计的字段。"""
        op = transfer.get("op")
        empty = {"payload": {}, "audit": {}}
        if op == "create":
            row = connection.execute("SELECT COALESCE(MAX(version),0) AS v FROM transfers WHERE record_id=?", (record_id,)).fetchone()
            version = int(row["v"]) + 1
            connection.execute(
                "INSERT INTO transfers(record_id,version,transfer_no,state,clues,reason,proposed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, version, None, "proposed", json.dumps(transfer["clues"], ensure_ascii=False), transfer["reason"], actor_id, now, now),
            )
            return {"payload": {"transfer_version": version}, "audit": {"transfer_version": version}}
        if op == "confirm":
            row = self._latest_transfer(connection, record_id, "proposed")
            transfer_no = transfer.get("transfer_no") or self._next_transfer_no(connection, now)
            try:
                connection.execute(
                    "UPDATE transfers SET state='confirmed',transfer_no=?,confirmed_by=?,updated_at=? WHERE id=?",
                    (transfer_no, actor_id, now, row["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("移送编号已存在，请更换") from exc
            return {"payload": {"transfer_no": transfer_no}, "audit": {"transfer_no": transfer_no, "transfer_version": row["version"]}}
        if op == "send":
            row = self._latest_transfer(connection, record_id, "confirmed")
            connection.execute(
                "UPDATE transfers SET state='sent',sent_to=?,sent_note=?,sent_by=?,sent_at=?,updated_at=? WHERE id=?",
                (transfer["sent_to"], transfer.get("note", ""), actor_id, now, now, row["id"]),
            )
            return {"payload": {}, "audit": {"transfer_no": row["transfer_no"], "sent_to": transfer["sent_to"]}}
        if op == "resolve":
            row = self._latest_transfer(connection, record_id, "sent")
            outcome = transfer["outcome"]
            connection.execute(
                "UPDATE transfers SET state=?,outcome=?,return_reason=?,resolved_by=?,resolved_at=?,updated_at=? WHERE id=?",
                ("accepted" if outcome == "accepted" else "returned", outcome, transfer.get("return_reason", ""), actor_id, now, now, row["id"]),
            )
            return {"payload": {}, "audit": {"transfer_no": row["transfer_no"], "outcome": outcome}}
        return empty

    def list_transfers(self, state: Optional[str] = None, record_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT t.*, r.reference AS record_reference, r.state AS record_state, r.payload AS record_payload FROM transfers t JOIN records r ON r.id=t.record_id"
        clauses: List[str] = []
        params: List[Any] = []
        if state:
            clauses.append("t.state=?")
            params.append(state)
        if record_id is not None:
            clauses.append("t.record_id=?")
            params.append(int(record_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._transfer_row(row) for row in rows]

    def record_transfers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.*, r.reference AS record_reference, r.state AS record_state, r.payload AS record_payload FROM transfers t JOIN records r ON r.id=t.record_id WHERE t.record_id=? ORDER BY t.version DESC",
                (record_id,),
            ).fetchall()
        return [self._transfer_row(row) for row in rows]

    def transfer_todo(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.*, r.reference AS record_reference, r.state AS record_state, r.payload AS record_payload "
                "FROM transfers t JOIN records r ON r.id=t.record_id "
                "WHERE t.state IN ('proposed','confirmed','sent') OR (t.state='returned' AND r.state='transfer_returned') "
                "ORDER BY t.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = [self._transfer_row(row) for row in rows]
        for item in items:
            item["todo"] = TRANSFER_TODO_LABELS.get(item["state"], "")
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
