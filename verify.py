"""verify —— 核验四点退出续办，并回归其余规则。

四点（子进程在 hook 处 os._exit(42) 模拟崩溃，父进程重开续办）：
  1. 中途片写盘未提交便退出 -> 重开截断未确认尾巴，续写不重不漏
  2. 末片写盘未确认便退出   -> 重开认盘上整片，补确认不漏
  3. 发布正文落盘未提交退出 -> 重开仍旧版，重投升同版
  4. 目录提交后退出         -> 重开见新版，重投领原版

任一检查失败以非零退出码报告。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from depot import (
    BadLineError,
    CorruptError,
    Depot,
    DuplicateLotError,
    NotFoundError,
    SourceError,
    SpecMismatchError,
    StateError,
)

CRASH_CODE = 42
CHILD = WORKSPACE / "crash_child.py"


def make_source(dirpath: Path, name: str, rows: list):
    """写 LF 结行 UTF-8 JSONL 源，返 (片名, 路径, sha256, 行数)。"""
    data = b"".join(
        json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in rows
    )
    path = dirpath / f"{name}.jsonl"
    path.write_bytes(data)
    return (name, path, hashlib.sha256(data).hexdigest(), len(rows))


def run_crash(root: Path, plan: dict, tmp: Path) -> None:
    """子进程按 plan 执行并在 hook 点崩溃；断言它确实死在指定点。"""
    plan_path = tmp / f"plan-{plan['id']}.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", str(CHILD), str(WORKSPACE), str(root), str(plan_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == CRASH_CODE, (
        f"子进程应以 {CRASH_CODE} 退出（崩溃在指定 hook 点），实际 "
        f"{proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def expect(exc_type, fn, what: str) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{what}: 应抛 {exc_type.__name__}，实际 {type(exc).__name__}: {exc}")
    raise AssertionError(f"{what}: 应抛 {exc_type.__name__}，未抛")


# ---------------------------------------------------------------- 四点退出续办

def point1_mid_part_uncommitted(tmp: Path) -> None:
    """点一：中途片写了一条、目录未提交便退出；重开截断续写，不重不漏。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [
        {"lot": "a", "material": "钢", "grams": 7},
        {"lot": "b", "material": "钢", "grams": 0},
    ])
    p2 = make_source(src, "p2", [
        {"lot": "c", "material": "铜", "grams": 5},
        {"lot": "d", "material": "铜", "grams": 3},
    ])
    spec = [[n, s, r] for n, _, s, r in (p1, p2)]
    run_crash(root, {
        "id": "b1", "parts": spec,
        "actions": [
            {"op": "step", "path": str(p1[1]), "n": 10},          # body 事件 1
            {"op": "step", "path": str(p2[1]), "n": 1},           # body 事件 2 -> 崩
        ],
        "crash": {"event": "body", "count": 2},
    }, tmp)

    d = Depot(root)  # 重开：截断 p2 未确认的一行
    st = d.status("b1")
    assert st["state"] == "open" and st["part"] == 1, st
    assert st["offset"] == 0 and st["rows"] == 0, st  # 未确认字节不认
    assert st["totals"] == {"钢": 7}, st

    st = d.step("b1", p2[1], 1)  # 续办重取第一行
    assert st["rows"] == 1 and st["totals"] == {"钢": 7, "铜": 5}, st
    st = d.step("b1", p2[1], 5)
    assert st["part"] == 2, st
    st = d.publish("b1")
    assert st["version"] == 1, st
    snap = d.snapshot()
    assert [r["lot"] for r in snap["rows"]] == ["a", "b", "c", "d"], snap
    assert snap["totals"] == {"钢": 7, "铜": 8}, snap
    d.close()


def point2_last_part_unconfirmed(tmp: Path) -> None:
    """点二：末片写盘未确认便退出；重开认盘上整片补确认，不漏不重。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [
        {"lot": "a", "material": "钢", "grams": 7},
        {"lot": "b", "material": "钢", "grams": 0},
    ])
    p2 = make_source(src, "p2", [{"lot": "c", "material": "铜", "grams": 5}])
    spec = [[n, s, r] for n, _, s, r in (p1, p2)]
    run_crash(root, {
        "id": "b2", "parts": spec,
        "actions": [
            {"op": "step", "path": str(p1[1]), "n": 10},          # body 事件 1
            {"op": "step", "path": str(p2[1]), "n": 10},          # 末片整片落盘 -> 崩
        ],
        "crash": {"event": "body", "count": 2},
    }, tmp)

    d = Depot(root)  # 重开：末片整片在盘上且摘要吻合 -> 补确认
    st = d.status("b2")
    assert st["state"] == "open" and st["part"] == 2, st  # 两片均已确认
    assert st["totals"] == {"钢": 7, "铜": 5}, st
    st = d.publish("b2")
    assert st["version"] == 1, st
    snap = d.snapshot()
    assert [r["lot"] for r in snap["rows"]] == ["a", "b", "c"], snap
    assert snap["totals"] == {"钢": 7, "铜": 5}, snap
    d.close()


def point3_publish_body_uncommitted(tmp: Path) -> None:
    """点三：发布正文落盘、目录未提交便退出；重开读旧版，重投升同版。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [
        {"lot": "a", "material": "钢", "grams": 7},
        {"lot": "b", "material": "钢", "grams": 0},
    ])
    p2 = make_source(src, "p2", [{"lot": "c", "material": "铜", "grams": 5}])
    spec = [[n, s, r] for n, _, s, r in (p1, p2)]
    run_crash(root, {
        "id": "b3", "parts": spec,
        "actions": [
            {"op": "step", "path": str(p1[1]), "n": 10},          # body 1
            {"op": "step", "path": str(p2[1]), "n": 10},          # body 2
            {"op": "publish"},                                     # body 3 -> 崩
        ],
        "crash": {"event": "body", "count": 3},
    }, tmp)

    d = Depot(root)  # 重开：发布未提交，现行仍是旧版
    st = d.status("b3")
    assert st["state"] == "open" and st["version"] is None, st
    snap = d.snapshot()
    assert snap == {"version": 0, "rows": [], "totals": {}}, snap  # 旧版为空

    st = d.publish("b3")  # 重投：升到同一个版本号
    assert st["version"] == 1, st
    snap = d.snapshot()
    assert snap["version"] == 1 and len(snap["rows"]) == 3, snap
    assert snap["totals"] == {"钢": 7, "铜": 5}, snap
    d.close()


def point4_publish_committed(tmp: Path) -> None:
    """点四：目录提交后退出；重开见新版，重投领原版。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [
        {"lot": "a", "material": "钢", "grams": 7},
        {"lot": "b", "material": "钢", "grams": 0},
    ])
    p2 = make_source(src, "p2", [{"lot": "c", "material": "铜", "grams": 5}])
    spec = [[n, s, r] for n, _, s, r in (p1, p2)]
    run_crash(root, {
        "id": "b4", "parts": spec,
        "actions": [
            {"op": "step", "path": str(p1[1]), "n": 10},          # commit 1
            {"op": "step", "path": str(p2[1]), "n": 10},          # commit 2
            {"op": "publish"},                                     # commit 3 -> 崩
        ],
        "crash": {"event": "commit", "count": 3},
    }, tmp)

    d = Depot(root)  # 重开：新版已生效
    st = d.status("b4")
    assert st["state"] == "published" and st["version"] == 1, st
    st = d.publish("b4")  # 重投领原版，不升新版
    assert st["version"] == 1, st
    assert d.snapshot()["version"] == 1
    snap = d.snapshot(1)
    assert [r["lot"] for r in snap["rows"]] == ["a", "b", "c"], snap
    assert snap["totals"] == {"钢": 7, "铜": 5}, snap
    d.close()


# ---------------------------------------------------------------- 规则回归

def rules_registry(tmp: Path) -> None:
    """同号同单返状态、异单拒绝、只一批在办、abort 永久停办。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [{"lot": "a", "material": "钢", "grams": 1}])
    p2 = make_source(src, "p2", [{"lot": "b", "material": "铜", "grams": 2}])
    spec = [[p1[0], p1[2], p1[3]]]
    d = Depot(root)
    st1 = d.start("b1", spec)
    st2 = d.start("b1", [tuple(x) for x in spec])  # 同号同单（tuple 也行）
    assert st1 == st2 and st2["state"] == "open", (st1, st2)
    expect(SpecMismatchError, lambda: d.start("b1", [[p2[0], p2[2], p2[3]]]),
           "异单拒绝")
    expect(StateError, lambda: d.start("b2", [[p2[0], p2[2], p2[3]]]),
           "只一批在办")
    expect(NotFoundError, lambda: d.status("nobody"), "未知批号")

    st = d.abort("b1")
    assert st["state"] == "aborted" and st["version"] is None, st
    expect(StateError, lambda: d.step("b1", p1[1], 1), "停办后不可收片")
    expect(StateError, lambda: d.publish("b1"), "停办后不可发布")
    st = d.start("b1", spec)  # 同号同单返状态：仍是已停办，不能复活
    assert st["state"] == "aborted", st
    st = d.abort("b1")  # 停办幂等
    assert st["state"] == "aborted", st
    st = d.start("b2", [[p2[0], p2[2], p2[3]]])  # 在办位空出，可开新批
    assert st["state"] == "open", st
    d.abort("b2")
    d.close()


def rules_source(tmp: Path) -> None:
    """错源、坏行、重复、行数不符均不推进；空片也确认；确认后源可移走。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    good = make_source(src, "good", [{"lot": "a", "material": "钢", "grams": 7}])
    other = make_source(src, "other", [{"lot": "z", "material": "锌", "grams": 1}])
    d = Depot(root)
    d.start("b1", [[good[0], good[2], good[3]]])
    expect(SourceError, lambda: d.step("b1", other[1], 1), "错源不推进")
    expect(SourceError, lambda: d.step("b1", src / "missing.jsonl", 1), "源缺失")
    st = d.status("b1")
    assert st["rows"] == 0 and st["offset"] == 0, st
    expect(ValueError, lambda: d.step("b1", good[1], 0), "n 非正整数")
    d.step("b1", good[1], 1)
    good[1].unlink()  # 收齐片源可移走
    st = d.publish("b1")  # 发布不再依赖源文件
    assert st["version"] == 1, st

    # 行数不符：登记 2 行，源只有 1 行
    d.start("b2", [[other[0], other[2], 2]])
    expect(SourceError, lambda: d.step("b2", other[1], 1), "行数不符不推进")
    d.abort("b2")

    # 坏行：非 JSON 行 / 缺键 / 负克数 / 多键 / CR 结行 / 空 lot / 克数为浮点
    def enc(text: str) -> bytes:
        return text.encode("utf-8")

    bad_cases = [
        enc('{"lot": "a", "material": "钢", "grams": 1}\nnot-json\n'),
        enc('{"lot": "a", "material": "钢"}\n'),
        enc('{"lot": "a", "material": "钢", "grams": -1}\n'),
        enc('{"lot": "a", "material": "钢", "grams": 1, "extra": 1}\n'),
        enc('{"lot": "a", "material": "钢", "grams": 1}\r\n'),
        enc('{"lot": "", "material": "钢", "grams": 1}\n'),
        enc('{"lot": "a", "material": "钢", "grams": 1.5}\n'),
    ]
    for i, data in enumerate(bad_cases):
        path = src / f"bad{i}.jsonl"
        path.write_bytes(data)
        rows = data.count(b"\n")
        d.start(f"bad{i}", [[f"bad{i}", hashlib.sha256(data).hexdigest(), rows]])
        expect(BadLineError, lambda p=path, b=f"bad{i}": d.step(b, p, rows),
               f"坏行不推进 #{i}")
        d.abort(f"bad{i}")

    # 批内 lot 重复（跨片）
    q1 = make_source(src, "q1", [{"lot": "dup", "material": "钢", "grams": 1}])
    q2 = make_source(src, "q2", [{"lot": "dup", "material": "铜", "grams": 2}])
    d.start("b3", [[q1[0], q1[2], 1], [q2[0], q2[2], 1]])
    d.step("b3", q1[1], 1)
    expect(DuplicateLotError, lambda: d.step("b3", q2[1], 1), "批内 lot 重复不推进")
    st = d.status("b3")
    assert st["part"] == 1 and st["rows"] == 0, st
    d.abort("b3")

    # 空片也确认
    empty = make_source(src, "empty", [])
    d.start("b4", [[empty[0], empty[2], 0]])
    st = d.step("b4", empty[1], 1)
    assert st["part"] == 1, st  # 空片一步确认
    st = d.publish("b4")
    assert st["version"] == 2, st
    assert d.snapshot(2) == {"version": 2, "rows": [], "totals": {}}
    d.close()


def rules_corruption(tmp: Path) -> None:
    """重开只认确认正文，缺损拒绝；坏版不出账。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [{"lot": "a", "material": "钢", "grams": 7}])
    p2 = make_source(src, "p2", [{"lot": "b", "material": "铜", "grams": 5}])
    d = Depot(root)
    d.start("b1", [[p1[0], p1[2], 1], [p2[0], p2[2], 1]])
    d.step("b1", p1[1], 1)
    confirmed_body = root / "staging" / "b1" / "part-0.body"
    assert confirmed_body.exists()
    confirmed_body.unlink()  # 已确认正文缺损
    d.close()
    expect(CorruptError, lambda: Depot(root), "确认正文缺损，重开拒绝")

    # 坏版不出账
    root2, src2 = tmp / "depot2", tmp / "src2"
    src2.mkdir()
    q1 = make_source(src2, "q1", [{"lot": "a", "material": "钢", "grams": 7}])
    d = Depot(root2)
    d.start("b1", [[q1[0], q1[2], 1]])
    d.step("b1", q1[1], 1)
    d.publish("b1")
    body = root2 / "bodies" / "v1.jsonl"
    body.write_bytes('{"lot": "x", "material": "钢", "grams": 999}\n'.encode("utf-8"))
    expect(CorruptError, lambda: d.snapshot(1), "坏版不出账")
    expect(NotFoundError, lambda: d.snapshot(9), "无此版本")
    expect(ValueError, lambda: d.snapshot(-1), "负版本号")
    d.close()


def rules_publish_atomic(tmp: Path) -> None:
    """齐片才发布；发布前读旧版；多批顺序升版。"""
    root, src = tmp / "depot", tmp / "src"
    src.mkdir()
    p1 = make_source(src, "p1", [{"lot": "a", "material": "钢", "grams": 7}])
    p2 = make_source(src, "p2", [{"lot": "b", "material": "铜", "grams": 5}])
    d = Depot(root)
    d.start("b1", [[p1[0], p1[2], 1], [p2[0], p2[2], 1]])
    expect(StateError, lambda: d.publish("b1"), "片未齐不发布")
    assert d.snapshot() == {"version": 0, "rows": [], "totals": {}}
    d.step("b1", p1[1], 1)
    d.step("b1", p2[1], 1)
    st = d.publish("b1")
    assert st["version"] == 1 and st["state"] == "published", st
    expect(StateError, lambda: d.abort("b1"), "已发批不可停办")

    q1 = make_source(src, "q1", [{"lot": "c", "material": "铝", "grams": 2}])
    d.start("b2", [[q1[0], q1[2], 1]])
    d.step("b2", q1[1], 1)
    assert d.snapshot()["version"] == 1  # 新批未发，现行仍是旧版
    st = d.publish("b2")
    assert st["version"] == 2, st
    assert d.snapshot()["version"] == 2
    assert d.snapshot(1)["totals"] == {"钢": 7, "铜": 5}  # 旧版仍在
    assert d.snapshot(2)["totals"] == {"铝": 2}
    d.close()


def main() -> int:
    points = [
        ("点一 中途片写盘未提交，重开截断续写不重不漏", point1_mid_part_uncommitted),
        ("点二 末片写盘未确认退出，重开补确认不漏", point2_last_part_unconfirmed),
        ("点三 发布正文落盘未提交，重开读旧版重投同版", point3_publish_body_uncommitted),
        ("点四 目录提交后退出，重开见新版重投领原版", point4_publish_committed),
    ]
    rules = [
        ("登记：同号同单/异单拒绝/只一批在办/停办", rules_registry),
        ("源与行：错源/坏行/重复/行数/空片/移源", rules_source),
        ("完整性：确认正文缺损拒绝/坏版不出账", rules_corruption),
        ("发布：齐片才升版/发布前读旧版/版本序列", rules_publish_atomic),
    ]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="depot-verify-") as base:
        base = Path(base)
        for i, (name, fn) in enumerate(points + rules, 1):
            case_dir = base / f"case{i}"
            case_dir.mkdir()
            try:
                fn(case_dir)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[失败] {name}\n       {type(exc).__name__}: {exc}")
            else:
                print(f"[通过] {name}")
    if failures:
        print(f"\n{failures} 项未通过")
        return 1
    print("\n全部核验通过：四点退出续办不漏不重，规则回归正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
