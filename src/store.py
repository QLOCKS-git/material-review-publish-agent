"""SQLite drafts with idempotent request/material identity and exclusive slots."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

class DraftStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, material_id TEXT NOT NULL,
                channel TEXT NOT NULL, proposed_at TEXT NOT NULL, original_hash TEXT NOT NULL,
                body TEXT NOT NULL, UNIQUE(request_id,material_id), UNIQUE(channel,proposed_at)
            )""")

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def all(self):
        with closing(self.connect()) as connection:
            return [json.loads(row["body"]) for row in connection.execute("SELECT body FROM drafts ORDER BY draft_id")]

    def reservations(self, request_id, material_id):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT channel,proposed_at FROM drafts WHERE NOT(request_id=? AND material_id=?)",
                (request_id, material_id))]

    def save(self, payload, original_hash, version_ok):
        try:
            return self._save(payload, original_hash, version_ok)
        except sqlite3.Error:
            return {"ok": False, "code": "SAVE_FAILED", "message": "草稿存储连接或事务失败。", "retryable": False}

    def _save(self, payload, original_hash, version_ok):
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM drafts WHERE request_id=? AND material_id=?",
                    (payload["request_id"], payload["material_id"])).fetchone()
                if existing and existing["original_hash"] != original_hash:
                    connection.rollback()
                    return {"ok": False, "code": "REQUEST_CHANGED", "message": "相同请求编号的硬性要求已变化，请使用新的 request_id。", "retryable": False}
                if not version_ok():
                    connection.rollback()
                    return {"ok": False, "code": "STALE_SCHEDULE", "message": "资料或排期已变化，请重新核验。", "retryable": True}
                previous = json.loads(existing["body"]) if existing else None
                body = dict(payload, draft_id=existing["draft_id"] if existing else str(uuid4()))
                if previous is not None and all(previous.get(k) == v for k, v in body.items()):
                    connection.commit()
                    return {"ok": True, "code": "UNCHANGED", "draft": previous}
                body["updated_at"] = datetime.now(timezone.utc).isoformat()
                connection.execute("""INSERT INTO drafts VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(request_id,material_id) DO UPDATE SET
                    channel=excluded.channel,proposed_at=excluded.proposed_at,body=excluded.body""",
                    (body["draft_id"], body["request_id"], body["material_id"], body["channel"],
                     body["proposed_at"], original_hash, json.dumps(body, ensure_ascii=False, sort_keys=True)))
                if not version_ok():
                    connection.rollback()
                    return {"ok": False, "code": "STALE_SCHEDULE", "message": "保存期间排期变化，请重新核验。", "retryable": True}
                connection.commit()
                return {"ok": True, "code": "UPDATED" if previous else "SAVED", "draft": body}
            except sqlite3.IntegrityError:
                connection.rollback()
                return {"ok": False, "code": "SLOT_CONFLICT", "message": "时段已被其他草稿占用，请重新查询。", "retryable": True}
            except sqlite3.Error:
                connection.rollback()
                return {"ok": False, "code": "SAVE_FAILED", "message": "草稿存储失败。", "retryable": False}
