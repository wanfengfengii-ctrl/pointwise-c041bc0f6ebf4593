"""depot —— 崩溃安全的料批收存库。

SQLite 只存目录（批、片、批内 lot、版本），正文一律落盘为文件：

    <root>/catalog.sqlite      目录
    <root>/staging/<批>/part-<i>.body   在办/已确认的分片正文
    <root>/bodies/v<N>.jsonl   已发布版本的正文

写序永远是"先正文落盘并 fsync，后目录事务提交"。进程在两者之间的
任意时刻退出，重开时 _reconcile() 只认目录已确认的字节：
  * 确认过的正文缺损 -> 拒绝（CorruptError）；
  * 未确认的尾巴若恰好是整片且摘要吻合 -> 补确认（不漏）；
  * 否则截断到目录记录的偏移（不重），续办时从源重取。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

__version__ = "1.0.0"

__all__ = [
    "Depot",
    "DepotError",
    "NotFoundError",
    "SpecMismatchError",
    "StateError",
    "SourceError",
    "BadLineError",
    "DuplicateLotError",
    "CorruptError",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id       TEXT PRIMARY KEY,
    state    TEXT NOT NULL CHECK (state IN ('open', 'published', 'aborted')),
    spec     TEXT NOT NULL,            -- JSON: [[片名, sha256, 行数], ...]
    version  INTEGER                   -- 已发布版本号，未发为 NULL
);
CREATE TABLE IF NOT EXISTS parts (
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    idx           INTEGER NOT NULL,
    name          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    expected_rows INTEGER NOT NULL,
    confirmed     INTEGER NOT NULL DEFAULT 0,
    offset        INTEGER NOT NULL DEFAULT 0,   -- 已确认的源字节数
    rows          INTEGER NOT NULL DEFAULT 0,   -- 已确认的源行数
    PRIMARY KEY (batch_id, idx)
);
CREATE TABLE IF NOT EXISTS lots (
    batch_id TEXT NOT NULL REFERENCES batches(id),
    lot      TEXT NOT NULL,
    PRIMARY KEY (batch_id, lot)
);
CREATE TABLE IF NOT EXISTS versions (
    version  INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL,
    rows     INTEGER NOT NULL,
    sha256   TEXT NOT NULL,
    totals   TEXT NOT NULL             -- JSON: {料名: 克数}
);
"""

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_LINE_KEYS = ("lot", "material", "grams")


class DepotError(Exception):
    """depot 领域错误基类。"""


class NotFoundError(DepotError):
    """批号或版本不存在。"""


class SpecMismatchError(DepotError):
    """同号异单。"""


class StateError(DepotError):
    """状态不允许该操作（在办冲突、已发、已停、片未齐等）。"""


class SourceError(DepotError):
    """错源或行数不符。"""


class BadLineError(SourceError):
    """坏行。"""


class DuplicateLotError(SourceError):
    """批内 lot 重复。"""


class CorruptError(DepotError):
    """确认过的正文缺损或坏版。"""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_line(raw: bytes) -> dict:
    """解析一行（不含 LF）。只接受 {lot, material, grams} 三键。"""
    if b"\r" in raw:
        raise BadLineError("行内出现 CR，源必须是 LF 结行")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BadLineError(f"非 UTF-8 行: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadLineError(f"非 JSON 行: {exc}") from exc
    if not isinstance(obj, dict) or set(obj.keys()) != set(_LINE_KEYS):
        raise BadLineError("行必须是且仅是 {lot, material, grams} 对象")
    lot, material, grams = obj["lot"], obj["material"], obj["grams"]
    if not isinstance(lot, str) or not lot:
        raise BadLineError("lot 必须是非空串")
    if not isinstance(material, str) or not material:
        raise BadLineError("material 必须是非空串")
    if isinstance(grams, bool) or not isinstance(grams, int) or grams < 0:
        raise BadLineError("grams 必须是非负整数")
    return {"lot": lot, "material": material, "grams": grams}


def _split_source(data: bytes, expected_rows: int, part_name: str) -> list:
    """整源校验：LF 结行 + 行数与登记相符。返回去 LF 的行字节列表。"""
    if data == b"":
        lines: list = []
    else:
        if not data.endswith(b"\n"):
            raise SourceError(f"片 {part_name!r}: 末行未以 LF 结束")
        lines = data[:-1].split(b"\n")
    if len(lines) != expected_rows:
        raise SourceError(
            f"片 {part_name!r}: 实际 {len(lines)} 行与登记 {expected_rows} 行不符"
        )
    return lines


def _validate_batch_id(batch_id) -> str:
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("批号必须是非空字符串")
    return batch_id


def _validate_parts(parts) -> list:
    if not isinstance(parts, (list, tuple)) or len(parts) == 0:
        raise ValueError("parts 必须是非空有序列表")
    spec = []
    seen = set()
    for i, item in enumerate(parts):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ValueError(f"第 {i} 片须为 (片名, SHA256, 行数) 三元组")
        name, sha, rows = item
        if not isinstance(name, str) or not name:
            raise ValueError(f"第 {i} 片片名必须是非空字符串")
        if name in seen:
            raise ValueError(f"片名重复: {name!r}")
        seen.add(name)
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise ValueError(f"片 {name!r}: SHA256 须为 64 位十六进制")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError(f"片 {name!r}: 行数须为非负整数")
        spec.append([name, sha.lower(), rows])
    return spec


def _add_totals(totals: dict, data: bytes) -> None:
    if not data:
        return
    for raw in data[:-1].split(b"\n"):
        rec = _parse_line(raw)
        totals[rec["material"]] = totals.get(rec["material"], 0) + rec["grams"]


class Depot:
    """建仓或重开。重开时自动续办在办批（对齐正文与目录）。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._bodies_dir.mkdir(exist_ok=True)
        self._staging_root.mkdir(exist_ok=True)
        self._db = sqlite3.connect(
            self.root / "catalog.sqlite", isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)
        # 清掉版本正文的原子写残骸（崩溃在 tmp 阶段）
        for tmp in self._bodies_dir.glob("*.tmp"):
            tmp.unlink()
        self._reconcile()

    # ---------- 路径 ----------

    @property
    def _bodies_dir(self) -> Path:
        return self.root / "bodies"

    @property
    def _staging_root(self) -> Path:
        return self.root / "staging"

    def _staging_dir(self, batch_id: str) -> Path:
        return self._staging_root / quote(batch_id, safe="")

    def _part_path(self, batch_id: str, idx: int) -> Path:
        return self._staging_dir(batch_id) / f"part-{idx}.body"

    def _version_path(self, version: int) -> Path:
        return self._bodies_dir / f"v{version}.jsonl"

    # ---------- 目录事务 ----------

    @contextmanager
    def _tx(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _get_batch(self, batch_id: str):
        row = self._db.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"未知批号 {batch_id!r}")
        return row

    def _get_parts(self, batch_id: str):
        return self._db.execute(
            "SELECT * FROM parts WHERE batch_id = ? ORDER BY idx", (batch_id,)
        ).fetchall()

    # ---------- 重开续办 ----------

    def _reconcile(self) -> None:
        batch = self._db.execute(
            "SELECT * FROM batches WHERE state = 'open'"
        ).fetchone()
        if batch is None:
            return
        batch_id = batch["id"]
        for part in self._get_parts(batch_id):
            path = self._part_path(batch_id, part["idx"])
            if part["confirmed"]:
                # 只认确认正文，缺损拒绝
                if not path.exists():
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 已确认正文缺失"
                    )
                if _sha256_file(path) != part["sha256"]:
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 已确认正文损坏"
                    )
                continue
            # 首个未确认片即当前片；其后的片按不变式尚未动过
            if not path.exists():
                if part["offset"] != 0:
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 在办正文缺失"
                    )
                return
            size = path.stat().st_size
            if size < part["offset"]:
                raise CorruptError(
                    f"批 {batch_id!r}: 片 {part['name']!r} 在办正文短于目录记录"
                )
            if size == part["offset"]:
                return
            # 盘上存在未确认的尾巴
            if _sha256_file(path) == part["sha256"]:
                # 末片写盘未确认便退出：整片已在盘上，补确认（不漏）
                data = path.read_bytes()
                try:
                    lines = _split_source(data, part["expected_rows"], part["name"])
                    records = [_parse_line(raw) for raw in lines]
                except SourceError:
                    records = None
                if records is not None and len({r["lot"] for r in records}) == len(records):
                    with self._tx():
                        for rec in records:
                            self._db.execute(
                                "INSERT OR IGNORE INTO lots (batch_id, lot) VALUES (?, ?)",
                                (batch_id, rec["lot"]),
                            )
                        self._db.execute(
                            "UPDATE parts SET offset = ?, rows = ?, confirmed = 1 "
                            "WHERE batch_id = ? AND idx = ?",
                            (size, part["expected_rows"], batch_id, part["idx"]),
                        )
                # 摘要吻合但内容不合登记：留给 step() 报 SourceError
                return
            # 半截或残缺的未确认尾巴：截断到目录偏移（不重）
            with open(path, "r+b") as fh:
                fh.truncate(part["offset"])
                fh.flush()
                os.fsync(fh.fileno())
            return

    # ---------- 登记 ----------

    def start(self, batch_id, parts):
        """登记一批。同号同单返状态，异单拒绝；只一批在办。"""
        batch_id = _validate_batch_id(batch_id)
        spec = _validate_parts(parts)
        with self._tx():
            row = self._db.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is not None:
                if json.loads(row["spec"]) != spec:
                    raise SpecMismatchError(
                        f"批 {batch_id!r} 已登记，片单不一致"
                    )
                return self._status_locked(batch_id)
            open_row = self._db.execute(
                "SELECT id FROM batches WHERE state = 'open'"
            ).fetchone()
            if open_row is not None:
                raise StateError(
                    f"批 {open_row['id']!r} 仍在办，只一批在办"
                )
            self._db.execute(
                "INSERT INTO batches (id, state, spec, version) VALUES (?, 'open', ?, NULL)",
                (batch_id, json.dumps(spec, ensure_ascii=False)),
            )
            for idx, (name, sha, rows) in enumerate(spec):
                self._db.execute(
                    "INSERT INTO parts (batch_id, idx, name, sha256, expected_rows) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (batch_id, idx, name, sha, rows),
                )
        self._staging_dir(batch_id).mkdir(parents=True, exist_ok=True)
        return self._status_locked(batch_id)

    # ---------- 收片 ----------

    def step(self, batch_id, path, n, hook=None):
        """收当前片至多 n 行。验全字节摘要与行数；坏行、重复不推进。"""
        batch_id = _validate_batch_id(batch_id)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("n 必须是正整数")
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise SourceError(f"读不到源 {path!r}: {exc}") from exc
        with self._tx():
            batch = self._get_batch(batch_id)
            if batch["state"] != "open":
                raise StateError(f"批 {batch_id!r} 状态为 {batch['state']}，不在办")
            part = self._db.execute(
                "SELECT * FROM parts WHERE batch_id = ? AND confirmed = 0 "
                "ORDER BY idx LIMIT 1",
                (batch_id,),
            ).fetchone()
            if part is None:
                raise StateError(f"批 {batch_id!r} 片已收齐，请 publish")
            if _sha256_bytes(data) != part["sha256"]:
                raise SourceError(f"片 {part['name']!r}: 源全字节摘要不符")
            lines = _split_source(data, part["expected_rows"], part["name"])
            records = []
            for raw in lines[part["rows"]: part["rows"] + n]:
                rec = _parse_line(raw)
                dup = self._db.execute(
                    "SELECT 1 FROM lots WHERE batch_id = ? AND lot = ?",
                    (batch_id, rec["lot"]),
                ).fetchone()
                if dup is not None:
                    raise DuplicateLotError(
                        f"批 {batch_id!r} 内 lot 重复: {rec['lot']!r}"
                    )
                records.append((raw, rec))
            body = b"".join(raw + b"\n" for raw, _ in records)
            part_file = self._part_path(batch_id, part["idx"])
            part_file.parent.mkdir(parents=True, exist_ok=True)
            with open(part_file, "ab") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            if hook is not None:
                hook("body")
            new_rows = part["rows"] + len(records)
            new_offset = part["offset"] + len(body)
            confirmed = 1 if new_rows == part["expected_rows"] else 0
            if confirmed and _sha256_file(part_file) != part["sha256"]:
                raise CorruptError(f"片 {part['name']!r}: 落盘正文摘要异常")
            for _, rec in records:
                self._db.execute(
                    "INSERT INTO lots (batch_id, lot) VALUES (?, ?)",
                    (batch_id, rec["lot"]),
                )
            self._db.execute(
                "UPDATE parts SET offset = ?, rows = ?, confirmed = ? "
                "WHERE batch_id = ? AND idx = ?",
                (new_offset, new_rows, confirmed, batch_id, part["idx"]),
            )
        if hook is not None:
            hook("commit")
        return self._status_locked(batch_id)

    # ---------- 停办 ----------

    def abort(self, batch_id):
        """永久停办未发批。"""
        batch_id = _validate_batch_id(batch_id)
        with self._tx():
            batch = self._get_batch(batch_id)
            if batch["state"] == "published":
                raise StateError(f"批 {batch_id!r} 已发布，不能停办")
            if batch["state"] == "open":
                self._db.execute(
                    "UPDATE batches SET state = 'aborted' WHERE id = ?", (batch_id,)
                )
        return self._status_locked(batch_id)

    # ---------- 发布 ----------

    def publish(self, batch_id, hook=None):
        """齐片才原子升版；重投领原版。"""
        batch_id = _validate_batch_id(batch_id)
        with self._tx():
            batch = self._get_batch(batch_id)
            if batch["state"] == "aborted":
                raise StateError(f"批 {batch_id!r} 已停办")
            if batch["state"] == "published":
                return self._status_locked(batch_id)  # 重投领原版
            parts = self._get_parts(batch_id)
            if any(not p["confirmed"] for p in parts):
                raise StateError(f"批 {batch_id!r} 片未收齐，不能发布")
            bodies = []
            for part in parts:
                part_file = self._part_path(batch_id, part["idx"])
                if not part_file.exists():
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 已确认正文缺失"
                    )
                data = part_file.read_bytes()
                if _sha256_bytes(data) != part["sha256"]:
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 已确认正文损坏"
                    )
                bodies.append(data)
            version = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM versions"
            ).fetchone()["v"]
            body_path = self._version_path(version)
            tmp_path = body_path.with_name(body_path.name + ".tmp")
            sha = hashlib.sha256()
            rows_total = 0
            totals: dict = {}
            with open(tmp_path, "wb") as out:
                for data in bodies:
                    sha.update(data)
                    out.write(data)
                    if data:
                        for raw in data[:-1].split(b"\n"):
                            rec = _parse_line(raw)
                            rows_total += 1
                            totals[rec["material"]] = (
                                totals.get(rec["material"], 0) + rec["grams"]
                            )
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_path, body_path)
            _fsync_dir(self._bodies_dir)
            if hook is not None:
                hook("body")
            self._db.execute(
                "INSERT INTO versions (version, batch_id, rows, sha256, totals) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    version,
                    batch_id,
                    rows_total,
                    sha.hexdigest(),
                    json.dumps(totals, ensure_ascii=False),
                ),
            )
            self._db.execute(
                "UPDATE batches SET state = 'published', version = ? WHERE id = ?",
                (version, batch_id),
            )
        if hook is not None:
            hook("commit")
        return self._status_locked(batch_id)

    # ---------- 查询 ----------

    def status(self, batch_id):
        """返 state、part（零基）、offset（源字节）、rows、totals、version。"""
        batch_id = _validate_batch_id(batch_id)
        return self._status_locked(batch_id)

    def _status_locked(self, batch_id: str) -> dict:
        batch = self._get_batch(batch_id)
        parts = self._get_parts(batch_id)
        confirmed = sum(1 for p in parts if p["confirmed"])
        current = parts[confirmed] if confirmed < len(parts) else None
        if batch["state"] == "published":
            vrow = self._db.execute(
                "SELECT totals FROM versions WHERE version = ?", (batch["version"],)
            ).fetchone()
            totals = json.loads(vrow["totals"])
        else:
            totals = self._totals_locked(batch_id, parts, confirmed)
        return {
            "state": batch["state"],
            "part": confirmed,
            "offset": current["offset"] if current is not None else 0,
            "rows": current["rows"] if current is not None else 0,
            "totals": totals,
            "version": batch["version"],
        }

    def _totals_locked(self, batch_id: str, parts, confirmed: int) -> dict:
        totals: dict = {}
        for part in parts[:confirmed]:
            path = self._part_path(batch_id, part["idx"])
            if not path.exists():
                raise CorruptError(
                    f"批 {batch_id!r}: 片 {part['name']!r} 已确认正文缺失"
                )
            _add_totals(totals, path.read_bytes())
        if confirmed < len(parts):
            part = parts[confirmed]
            if part["offset"]:
                path = self._part_path(batch_id, part["idx"])
                if not path.exists():
                    raise CorruptError(
                        f"批 {batch_id!r}: 片 {part['name']!r} 在办正文缺失"
                    )
                with open(path, "rb") as fh:
                    _add_totals(totals, fh.read(part["offset"]))
        return totals

    def snapshot(self, v=None):
        """返指定或现行版的 version、原序 rows、totals。0 版为空，坏版不出账。"""
        if v is None:
            row = self._db.execute("SELECT MAX(version) AS v FROM versions").fetchone()
            v = row["v"] or 0
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("版本号须为非负整数")
        if v == 0:
            return {"version": 0, "rows": [], "totals": {}}
        row = self._db.execute(
            "SELECT * FROM versions WHERE version = ?", (v,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"无此版本 {v}")
        body_path = self._version_path(v)
        if not body_path.exists():
            raise CorruptError(f"版本 {v} 正文缺失")
        data = body_path.read_bytes()
        if _sha256_bytes(data) != row["sha256"]:
            raise CorruptError(f"版本 {v} 正文损坏，不出账")
        rows = [] if not data else [_parse_line(raw) for raw in data[:-1].split(b"\n")]
        totals: dict = {}
        for rec in rows:
            totals[rec["material"]] = totals.get(rec["material"], 0) + rec["grams"]
        return {"version": v, "rows": rows, "totals": totals}

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Depot":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
