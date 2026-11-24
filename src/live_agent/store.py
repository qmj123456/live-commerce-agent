import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, kind TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS claims(key TEXT PRIMARY KEY, state TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS sends(at REAL, account TEXT, target TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS bag_runs(id TEXT PRIMARY KEY, cost INTEGER, state TEXT);
        ''')
        # Operations interrupted across restart are uncertain, never replay blindly.
        self.db.execute("UPDATE claims SET state='uncertain' WHERE state='executing'")
        self.db.commit()

    def event(self, kind, payload):
        self.db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)",
                        (time.time(), kind, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def save(self, candidate):
        self.db.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                        (candidate.id, candidate.status, candidate.model_dump_json()))
        self.db.commit()

    def candidates(self):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM candidates ORDER BY rowid DESC LIMIT 100")]

    def claim(self, key):
        cursor = self.db.execute("INSERT OR IGNORE INTO claims VALUES(?,?,?)",
                                 (key, "executing", time.time()))
        self.db.commit()
        return cursor.rowcount == 1

    def finish(self, key, state):
        self.db.execute("UPDATE claims SET state=? WHERE key=?", (state, key))
        self.db.commit()

    def record_send(self, account, target, text):
        self.db.execute("INSERT INTO sends VALUES(?,?,?,?)", (time.time(), account, target, text))
        self.db.commit()

    def rate_wait(self, account, target, text, per_minute):
        now = time.time()
        rows = list(self.db.execute("SELECT at,target,text FROM sends WHERE account=? AND at>? ORDER BY at",
                                    (account, now - 1800)))
        waits = [0.0]
        recent = [r for r in rows if r[0] > now - 60]
        if len(recent) >= per_minute:
            waits.append(recent[-per_minute][0] + 60 - now)
        for at, previous_target, previous_text in rows:
            if target and previous_target == target:
                waits.append(at + 120 - now)
            if previous_text == text:
                waits.append(at + 1800 - now)
        if rows:
            waits.append(rows[-1][0] + 2 - now)
        return max(waits)

    def reserve_bag(self, run_id, cost, budget, max_runs):
        with self.db:
            total, count = self.db.execute(
                "SELECT COALESCE(SUM(cost),0),COUNT(*) FROM bag_runs").fetchone()
            if total + cost > budget or count >= max_runs:
                raise ValueError("福袋预算或次数超限")
            self.db.execute("INSERT INTO bag_runs VALUES(?,?,?)", (run_id, cost, "reserved"))

    def bag_state(self, run_id, state):
        self.db.execute("UPDATE bag_runs SET state=? WHERE id=?", (state, run_id))
        self.db.commit()

    def close(self):
        self.db.close()
