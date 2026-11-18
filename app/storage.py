"""SQLite 存储层。

并发/一致性设计：
- 每个 trip 有单调递增 revision；确认提交采用条件更新
  UPDATE trip SET revision=? WHERE id=? AND revision=?，配合 BEGIN IMMEDIATE
  写事务，同一 revision 的并发确认只有一个事务能成功（其余提交 0 行 -> 409）。
- 草稿（预览）带一次性令牌 draft_id：每次保存草稿都生成新令牌，预览页表单
  携带该令牌，确认在事务内核对——令牌不匹配说明页面已被新修订替换，
  确认被拒绝，正式版保持原状。确认只能发布该页面所见的数据。
- 草稿的两份 CSV 原文、读数/装卸与计算结果在单个事务内写入同一行，
  不会留下"结果与原文分属不同修订"的混合草稿；确认提交在单事务内
  整趟替换正式数据，并按令牌条件删除草稿——确认与修订交错时，
  已保存成功的新草稿不会被旧确认误删。
- 每次确认在单个事务内整趟替换：删除旧 readings/rides/results 后重插，
  任何一步失败全部回滚；查询永远只能看到完整旧版或完整新版。
- WAL 模式 + busy_timeout，兼顾读写并发与重启后的持久化。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trip (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    temp_limit      REAL NOT NULL,
    temp_csv        TEXT NOT NULL DEFAULT '',
    handling_csv    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    confirmed_at    TEXT
);
CREATE TABLE IF NOT EXISTS trip_reading (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    celsius     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_ride (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    box         TEXT NOT NULL,
    loaded_at   INTEGER NOT NULL,
    unloaded_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE TABLE IF NOT EXISTS trip_result (
    trip_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    box             TEXT NOT NULL,
    hot_seconds     INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL,
    total_seconds   INTEGER NOT NULL,
    hot_segments    TEXT NOT NULL,
    unknown_segments TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE INDEX IF NOT EXISTS idx_ride_trip ON trip_ride(trip_id, revision);
CREATE INDEX IF NOT EXISTS idx_result_trip ON trip_result(trip_id, revision);
CREATE TABLE IF NOT EXISTS trip_draft (
    trip_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    temp_limit REAL NOT NULL,
    temp_csv TEXT NOT NULL,
    handling_csv TEXT NOT NULL,
    readings TEXT NOT NULL,
    rides TEXT NOT NULL,
    results TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class RevisionConflict(RuntimeError):
    """同一 revision 的确认已被其他事务抢先提交，或修订基线已过期。"""


class DraftConflict(RuntimeError):
    """确认所针对的草稿已被新的修订替换（预览页过期）。"""


class NotFound(LookupError):
    pass


def get_db_path() -> Path:
    from flask import current_app

    return Path(current_app.config["DATABASE"])


def _connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None：全程手动事务控制，避免 SELECT 隐式开启的事务
    # 让同一连接上的多条语句停留在旧快照，读到跨版本的混合数据
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | Path) -> None:
    """建表 + 结构迁移，整体在单个 IMMEDIATE 事务内完成（多 worker 启动时串行）。

    旧版把草稿拆成 trip_draft / trip_draft_source 两表分两事务写入，
    失败或重启后可能留下"结果与 CSV 原文分属不同修订"的混合草稿。
    草稿属于临时数据，结构升级时直接重建；正式版本数据不动。
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(trip_draft)")}
        if draft_cols and "draft_id" not in draft_cols:
            conn.execute("DROP TABLE trip_draft")
        # 旧旁路表已并入 trip_draft，一律清除，杜绝混合草稿残留
        conn.execute("DROP TABLE IF EXISTS trip_draft_source")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        trip_cols = {r[1] for r in conn.execute("PRAGMA table_info(trip)")}
        if "temp_csv" not in trip_cols:
            conn.execute(
                "ALTER TABLE trip ADD COLUMN temp_csv TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE trip ADD COLUMN handling_csv TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor(commit: bool = False):
    conn = _connect(get_db_path())
    try:
        if commit:
            # 写事务立即拿写锁，配合条件更新保证同修订号并发确认只成功一次
            conn.execute("BEGIN IMMEDIATE")
        else:
            # 显式只读事务：一次查询内的多条 SELECT 共享同一快照，
            # 绝不可能读到"新表头 + 旧明细"之类的跨版本混合状态
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 草稿（待确认的修订预览） ----------

def upsert_draft(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    temp_csv: str,
    handling_csv: str,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
) -> str:
    """保存草稿（同一 trip 反复修正只保留最新一份），返回草稿令牌 draft_id。

    两份 CSV 原文、读数/装卸与计算结果在单个事务内写入同一行，
    始终属于同一次修订；失败或重启不会留下混合草稿。
    事务内校验基线修订号：若正式版已被并发确认推进，则拒绝写入，
    避免"提示成功却永远确认不了"的过期草稿。
    """
    draft_id = uuid.uuid4().hex
    with db_cursor(commit=True) as conn:
        row = conn.execute("SELECT revision FROM trip WHERE id=?", (trip_id,)).fetchone()
        current = row["revision"] if row else 0
        if current != expected_revision:
            raise RevisionConflict(
                f"正式版本已推进到 rev {current}，基于 rev {expected_revision} "
                "的修订已过期，请基于最新版本重新修正"
            )
        conn.execute(
            "INSERT INTO trip_draft(trip_id, draft_id, expected_revision, name, temp_limit, "
            "temp_csv, handling_csv, readings, rides, results) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trip_id) DO UPDATE SET draft_id=excluded.draft_id, "
            "expected_revision=excluded.expected_revision, "
            "name=excluded.name, temp_limit=excluded.temp_limit, "
            "temp_csv=excluded.temp_csv, handling_csv=excluded.handling_csv, "
            "readings=excluded.readings, rides=excluded.rides, results=excluded.results, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (
                trip_id, draft_id, expected_revision, name, temp_limit,
                temp_csv, handling_csv,
                json.dumps(readings, separators=(",", ":")),
                json.dumps(rides, separators=(",", ":")),
                json.dumps(results, separators=(",", ":")),
            ),
        )
    return draft_id


def get_draft(trip_id: str) -> dict | None:
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM trip_draft WHERE trip_id=?", (trip_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "trip_id": row["trip_id"],
        "draft_id": row["draft_id"],
        "expected_revision": row["expected_revision"],
        "name": row["name"],
        "temp_limit": row["temp_limit"],
        "temp_csv": row["temp_csv"],
        "handling_csv": row["handling_csv"],
        "readings": json.loads(row["readings"]),
        "rides": json.loads(row["rides"]),
        "results": json.loads(row["results"]),
    }


# ---------- 正式版本（已确认结论，只增不改） ----------

def commit_revision(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
    *,
    temp_csv: str = "",
    handling_csv: str = "",
    draft_token: str | None = None,
) -> int:
    """整趟替换提交。返回新 revision。

    - expected_revision 过期（正式版已被推进）则抛 RevisionConflict；
    - 传入 draft_token 时，事务内核对当前草稿令牌：令牌不匹配说明预览页
      已被新修订替换（或草稿已被确认），抛 DraftConflict，正式版保持原状；
    - 草稿删除与确认同事务，且按令牌条件删除：交错期间保存成功的新草稿
      不会被这次旧确认误删。
    新 trip（expected_revision=0 且不存在）创建为 revision 1。
    """
    with db_cursor(commit=True) as conn:
        if draft_token is not None:
            row = conn.execute(
                "SELECT draft_id FROM trip_draft WHERE trip_id=?", (trip_id,)
            ).fetchone()
            if row is None:
                raise DraftConflict("待确认的预览已不存在（可能已被确认或替换）")
            if row["draft_id"] != draft_token:
                raise DraftConflict("预览已被新的修订替换，本次确认作废")

        trip = conn.execute("SELECT revision FROM trip WHERE id=?", (trip_id,)).fetchone()
        current = trip["revision"] if trip else 0
        if current != expected_revision:
            raise RevisionConflict(
                f"修订号已过期：页面基于 rev {expected_revision}，当前为 rev {current}"
            )
        new_revision = current + 1

        if trip is None:
            conn.execute(
                "INSERT INTO trip(id, name, revision, temp_limit, temp_csv, handling_csv, "
                "confirmed_at) VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (trip_id, name, new_revision, temp_limit, temp_csv, handling_csv),
            )
        else:
            updated = conn.execute(
                "UPDATE trip SET revision=?, name=?, temp_limit=?, temp_csv=?, handling_csv=?, "
                "confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND revision=?",
                (new_revision, name, temp_limit, temp_csv, handling_csv, trip_id, current),
            ).rowcount
            if updated != 1:  # 并发下另一事务已推进 revision
                raise RevisionConflict("同一修订号的并发确认只允许一次")
            # 整趟替换：先删旧数据（含已删除箱号的旧结论），再插新版
            for table in ("trip_reading", "trip_ride", "trip_result"):
                conn.execute(f"DELETE FROM {table} WHERE trip_id=?", (trip_id,))

        conn.executemany(
            "INSERT INTO trip_reading(trip_id, revision, ts, celsius) VALUES(?,?,?,?)",
            [(trip_id, new_revision, ts, c) for ts, c in readings],
        )
        conn.executemany(
            "INSERT INTO trip_ride(trip_id, revision, box, loaded_at, unloaded_at) "
            "VALUES(?,?,?,?,?)",
            [(trip_id, new_revision, box, lo, ul) for box, lo, ul in rides],
        )
        conn.executemany(
            "INSERT INTO trip_result(trip_id, revision, box, hot_seconds, "
            "unknown_seconds, total_seconds, hot_segments, unknown_segments) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    trip_id, new_revision, box, r["hot_seconds"], r["unknown_seconds"],
                    r["total_seconds"],
                    json.dumps(r["hot_segments"], separators=(",", ":")),
                    json.dumps(r["unknown_segments"], separators=(",", ":")),
                )
                for box, r in results.items()
            ],
        )
        if draft_token is not None:
            # 只删除本次确认的这份草稿；期间出现的更新草稿（令牌不同）保留
            conn.execute(
                "DELETE FROM trip_draft WHERE trip_id=? AND draft_id=?",
                (trip_id, draft_token),
            )
        else:
            conn.execute("DELETE FROM trip_draft WHERE trip_id=?", (trip_id,))
        return new_revision


def get_trip(trip_id: str) -> dict:
    with db_cursor() as conn:
        trip = conn.execute("SELECT * FROM trip WHERE id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFound(f"运输趟次不存在：{trip_id}")
        revision = trip["revision"]
        rides = conn.execute(
            "SELECT box, loaded_at, unloaded_at FROM trip_ride "
            "WHERE trip_id=? AND revision=? ORDER BY box", (trip_id, revision)
        ).fetchall()
        results = conn.execute(
            "SELECT * FROM trip_result WHERE trip_id=? AND revision=? ORDER BY box",
            (trip_id, revision),
        ).fetchall()
        readings = conn.execute(
            "SELECT ts, celsius FROM trip_reading WHERE trip_id=? AND revision=? ORDER BY ts",
            (trip_id, revision),
        ).fetchall()
    return {
        "id": trip["id"],
        "name": trip["name"],
        "revision": revision,
        "temp_limit": trip["temp_limit"],
        "temp_csv": trip["temp_csv"],
        "handling_csv": trip["handling_csv"],
        "created_at": trip["created_at"],
        "confirmed_at": trip["confirmed_at"],
        "readings": [(r["ts"], r["celsius"]) for r in readings],
        "rides": [dict(r) for r in rides],
        "results": [
            {
                "box": r["box"],
                "hot_seconds": r["hot_seconds"],
                "unknown_seconds": r["unknown_seconds"],
                "total_seconds": r["total_seconds"],
                "hot_segments": json.loads(r["hot_segments"]),
                "unknown_segments": json.loads(r["unknown_segments"]),
            }
            for r in results
        ],
    }


def list_trips() -> list[dict]:
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, t.revision, t.temp_limit, t.confirmed_at, "
            "(SELECT COUNT(*) FROM trip_ride r WHERE r.trip_id=t.id AND r.revision=t.revision) AS boxes "
            "FROM trip t ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
