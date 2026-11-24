"""Persistent sqlite-vec KNN index, with atomic snapshots and product/expiry prefilters."""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

import sqlite_vec


def fact_hash(fact):
    return hashlib.sha256(fact.model_dump_json().encode()).hexdigest()


class VectorStore:
    def __init__(self, path, dimensions):
        if not 1 <= dimensions <= 8192:
            raise ValueError("向量维度超出支持范围")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        try:
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
        except Exception:
            self.db.close()
            raise
        finally:
            # Only our installed native extension may be loaded.
            try:
                self.db.enable_load_extension(False)
            except sqlite3.ProgrammingError:
                pass
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY, fact_id TEXT UNIQUE NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL
          );
        ''')
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='vectors'").fetchone():
            self.create_vectors(dimensions)
            self.db.commit()

    def create_vectors(self, dimensions):
        # Dimensions are a validated integer, never user-provided SQL.
        self.db.execute(f'''CREATE VIRTUAL TABLE vectors USING vec0(
            embedding float[{int(dimensions)}] distance_metric=cosine,
            product_id text, valid_until float
        )''')

    def metadata(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def ready(self, config, fingerprint):
        return self.metadata("config") == config and self.metadata("fingerprint") == fingerprint

    def changed(self, facts, config):
        if self.metadata("config") != config:
            return list(facts)
        hashes = dict(self.db.execute("SELECT fact_id,hash FROM documents"))
        return [f for f in facts if hashes.get(f.id) != fact_hash(f)]

    @staticmethod
    def validate(vector, dimensions):
        if (len(vector) != dimensions or not all(math.isfinite(v) for v in vector)
                or not any(v != 0 for v in vector)):
            raise ValueError("Embedding 返回了维度错误、非有限值或零向量")
        return sqlite_vec.serialize_float32(vector)

    def replace(self, facts, changed, embeddings, config, fingerprint):
        if len(changed) != len(embeddings):
            raise ValueError("Embedding 返回数量与知识条目数不符")
        packed = [self.validate(v, config["dimensions"]) for v in embeddings]
        wanted = {f.id for f in facts}
        # No network requests occur inside this transaction. Readers see old or new snapshot.
        with self.db:
            if self.metadata("config") != config:
                self.db.execute("DROP TABLE vectors")
                self.create_vectors(config["dimensions"])
                self.db.execute("DELETE FROM documents")
            for row_id, fact_id in self.db.execute("SELECT id,fact_id FROM documents").fetchall():
                if fact_id not in wanted:
                    self.db.execute("DELETE FROM vectors WHERE rowid=?", (row_id,))
                    self.db.execute("DELETE FROM documents WHERE id=?", (row_id,))
            for fact, blob in zip(changed, packed):
                old = self.db.execute("SELECT id FROM documents WHERE fact_id=?", (fact.id,)).fetchone()
                if old:
                    row_id = old[0]
                    self.db.execute("DELETE FROM vectors WHERE rowid=?", (row_id,))
                    self.db.execute("UPDATE documents SET hash=?,payload=? WHERE id=?",
                                    (fact_hash(fact), fact.model_dump_json(), row_id))
                else:
                    row_id = self.db.execute("INSERT INTO documents(fact_id,hash,payload) VALUES(?,?,?)",
                        (fact.id, fact_hash(fact), fact.model_dump_json())).lastrowid
                expires = fact.valid_until.timestamp() if fact.valid_until.tzinfo is not None else 0.0
                self.db.execute("INSERT INTO vectors(rowid,embedding,product_id,valid_until) VALUES(?,?,?,?)",
                                (row_id, blob, fact.product_id, float(expires)))
            for key, value in (("config", config), ("fingerprint", fingerprint)):
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))

    def search(self, vector, product_id, dimensions, threshold, top_k=5):
        blob = self.validate(vector, dimensions)
        matches = []
        # Separate KNN queries avoid unsupported OR clauses; both filters run inside vec0.
        for scope in dict.fromkeys((product_id, "*")):
            rows = self.db.execute('''SELECT rowid,distance FROM vectors
                WHERE embedding MATCH ? AND k=? AND product_id=? AND valid_until>?
                ORDER BY distance''', (blob, top_k, scope, time.time())).fetchall()
            for row_id, distance in rows:
                if 1.0 - distance >= threshold:
                    fact_id = self.db.execute("SELECT fact_id FROM documents WHERE id=?", (row_id,)).fetchone()[0]
                    matches.append((distance, fact_id))
        return [key for _, key in sorted(matches)[:top_k]]

    def status(self):
        return {"backend": "sqlite-vec", "version": self.db.execute("SELECT vec_version()").fetchone()[0],
                "vectors": self.db.execute("SELECT count(*) FROM documents").fetchone()[0]}

    def close(self):
        self.db.close()
