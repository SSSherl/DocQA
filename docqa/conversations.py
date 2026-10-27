"""Local conversation history. Prior messages remain untrusted context, not evidence."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Conversations:
    def __init__(self, root):
        self.path = root / "conversations.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS turns(id INTEGER PRIMARY KEY,conversation_id TEXT,question TEXT,result TEXT,created REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id,id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connect() as db:
            return [{"id":r[0], "title":r[1], "updated":r[2], "turns":r[3]} for r in db.execute("SELECT c.id,c.title,c.updated,COUNT(t.id) FROM conversations c JOIN turns t ON t.conversation_id=c.id GROUP BY c.id ORDER BY c.updated DESC LIMIT 100")]

    def get(self, conversation_id):
        with self.connect() as db:
            item = db.execute("SELECT id,title,updated FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not item:
                raise ValueError("会话不存在或已删除")
            turns = [{"id":r[0], "question":r[1], "result":json.loads(r[2]), "created":r[3]} for r in db.execute("SELECT id,question,result,created FROM turns WHERE conversation_id=? ORDER BY id", (conversation_id,))]
        return {"id":item[0], "title":item[1], "updated":item[2], "turns":turns}

    def append(self, conversation_id, question, result):
        if conversation_id:
            self.get(conversation_id)
        else:
            conversation_id = uuid.uuid4().hex
        now = time.time()
        # Persist raw Markdown only; restored HTML is always sanitized again.
        raw = {k:v for k,v in result.items() if k not in {"answer_html", "conversation_id", "turn_id"}}
        raw["citations"] = [{k:v for k,v in c.items() if k != "excerpt_html"} for c in raw.get("citations", [])]
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO conversations VALUES(?,?,?)", (conversation_id,question[:70],now))
            db.execute("UPDATE conversations SET updated=? WHERE id=?", (now,conversation_id))
            cursor = db.execute("INSERT INTO turns(conversation_id,question,result,created) VALUES(?,?,?,?)", (conversation_id,question,json.dumps(raw,ensure_ascii=False),now))
            turn_id = cursor.lastrowid
        return {"conversation_id":conversation_id, "turn_id":turn_id}

    def delete(self, conversation_id):
        with self.connect() as db:
            db.execute("DELETE FROM turns WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
