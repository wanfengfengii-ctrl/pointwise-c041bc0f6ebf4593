"""崩溃模拟子进程：verify.py 专用。

用法: python crash_child.py <workspace> <root> <plan.json>
在 hook 收到指定事件第 N 次时 os._exit(42)，模拟进程崩溃退出。
"""

import json
import os
import sys

CRASH_CODE = 42


def main() -> None:
    workspace, root, plan_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, workspace)
    from depot import Depot

    with open(plan_path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    crash = plan.get("crash") or {}
    counts = {}

    def hook(event):
        if event == crash.get("event"):
            counts[event] = counts.get(event, 0) + 1
            if counts[event] == crash.get("count"):
                os._exit(CRASH_CODE)

    depot = Depot(root)
    depot.start(plan["id"], plan["parts"])
    for act in plan["actions"]:
        if act["op"] == "step":
            depot.step(plan["id"], act["path"], act["n"], hook=hook)
        elif act["op"] == "publish":
            depot.publish(plan["id"], hook=hook)
        else:
            raise ValueError(f"未知动作 {act!r}")


if __name__ == "__main__":
    main()
