"""SQLite 持久层：字体、任务、发现、分段、链间差异五张表。

WAL 模式 + 每次操作独立连接，保证后台分析线程与 API 请求并发安全；
数据库文件落盘，服务重启后任务与结果仍可查询、可重跑。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS fonts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  format TEXT,
  family TEXT,
  subfamily TEXT,
  num_glyphs INTEGER,
  num_cmap INTEGER,
  has_cmap14 INTEGER DEFAULT 0,
  uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  chain TEXT NOT NULL,
  kind TEXT NOT NULL,
  text_index INTEGER NOT NULL,
  lang TEXT,
  start INTEGER NOT NULL,
  "end" INTEGER NOT NULL,
  cluster TEXT,
  codepoints TEXT,
  script TEXT,
  font_id INTEGER,
  related_font_id INTEGER,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_task ON findings(task_id, kind);
CREATE INDEX IF NOT EXISTS idx_findings_script ON findings(task_id, script);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  chain TEXT NOT NULL,
  text_index INTEGER NOT NULL,
  start INTEGER NOT NULL,
  "end" INTEGER NOT NULL,
  text TEXT,
  font_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_segments_task ON segments(task_id);
CREATE TABLE IF NOT EXISTS diffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  text_index INTEGER NOT NULL,
  start INTEGER NOT NULL,
  "end" INTEGER NOT NULL,
  cluster TEXT,
  codepoints TEXT,
  script TEXT,
  kind TEXT NOT NULL,
  chain_a TEXT,
  chain_b TEXT,
  repro TEXT,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_diffs_task ON diffs(task_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._conn() as con:
            cur = con.execute(sql, tuple(params))
            return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._conn() as con:
            con.executemany(sql, rows)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._conn() as con:
            return [dict(r) for r in con.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
