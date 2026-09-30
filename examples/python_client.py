"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))


if __name__ == "__main__":
    main()
