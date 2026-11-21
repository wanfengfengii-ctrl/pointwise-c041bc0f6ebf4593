"""demo —— 样例演示。

首片 a钢7、b钢0，次片 c铜5；首步限一行；发布得三行、钢7铜5。
同时展示：确认后源可移走、发布前读旧版、重投领原版、重开版本仍在。
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from depot import Depot


def write_source(dirpath: Path, name: str, rows: list):
    data = b"".join(
        json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in rows
    )
    path = dirpath / f"{name}.jsonl"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest(), len(rows)


def show(title: str, obj) -> None:
    print(f"  {title}: {json.dumps(obj, ensure_ascii=False)}")


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="depot-demo-"))
    src = work / "src"
    src.mkdir()
    root = work / "depot"

    p1, sha1, n1 = write_source(src, "首片", [
        {"lot": "a", "material": "钢", "grams": 7},
        {"lot": "b", "material": "钢", "grams": 0},
    ])
    p2, sha2, n2 = write_source(src, "次片", [
        {"lot": "c", "material": "铜", "grams": 5},
    ])

    print(f"建仓于 {root}")
    depot = Depot(root)
    print("登记批 batch-1：首片 2 行 + 次片 1 行")
    show("start", depot.start("batch-1", [("首片", sha1, n1), ("次片", sha2, n2)]))

    print("\n首步限一行（n=1）：")
    show("step n=1", depot.step("batch-1", p1, 1))

    print("\n收齐首片余下行：")
    show("step n=10", depot.step("batch-1", p1, 10))

    p1.unlink()
    print("\n首片已确认，源文件移走（删除），续办不受影响")

    print("\n发布前读现行版（旧版为空）：")
    show("snapshot()", depot.snapshot())

    print("\n收次片：")
    show("step n=5", depot.step("batch-1", p2, 5))

    print("\n发布：")
    show("publish", depot.publish("batch-1"))

    print("\n现行版（三行、钢7铜5）：")
    show("snapshot()", depot.snapshot())

    print("\n旧版（0 版仍为空）：")
    show("snapshot(0)", depot.snapshot(0))

    print("\n重投领原版（版本不变）：")
    show("publish again", depot.publish("batch-1"))
    depot.close()

    print("\n重开仓，版本仍在：")
    depot2 = Depot(root)
    show("snapshot(1)", depot2.snapshot(1))
    show("status", depot2.status("batch-1"))
    depot2.close()
    print(f"\n演示数据目录：{work}")


if __name__ == "__main__":
    main()
