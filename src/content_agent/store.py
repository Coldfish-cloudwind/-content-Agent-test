"""One SQLite row is an atomic checkpoint including the plan, charges and events."""
import json
import sqlite3
from pathlib import Path


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)")

    def close(self):
        self.db.close()

    def get(self, key):
        row = self.db.execute("SELECT body FROM campaigns WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, state):
        key = state["request"]["campaign_id"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO campaigns VALUES(?,?,?)",
                            (key, 0, json.dumps(state, ensure_ascii=False)))
        return self.get(key)

    def commit(self, state):
        key = state["request"]["campaign_id"]
        previous = state["revision"]
        state["revision"] += 1
        with self.db:
            cur = self.db.execute("UPDATE campaigns SET revision=?, body=? WHERE id=? AND revision=?",
                                  (state["revision"], json.dumps(state, ensure_ascii=False), key, previous))
            if cur.rowcount != 1:
                raise Conflict("状态已更新。丢弃过期动作，请重新读取当前版本。")
