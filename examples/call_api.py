#!/usr/bin/env python3
"""冷链追责服务调用示例（仅用标准库，无需安装 requests）。

用法：
    BASE_URL=http://localhost:8000 python3 examples/call_api.py
"""
import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


def call(method: str, path: str, body: dict | None = None):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
            print(f"{method} {path} -> {resp.status}")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return resp.status, payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(f"{method} {path} -> {e.code} {detail}")
        return e.code, json.loads(detail)


def main():
    # 1. 创建货物：编号、起始时间、温度上限、起始保管方
    call("POST", "/api/cargo", {
        "cargo_no": "DEMO-1",
        "start_time": "2026-01-01T00:00:00Z",
        "temp_upper_limit": "5.0",
        "initial_holder": "WAREHOUSE-A",
    })

    # 2. 测温：01:00 超限开启区间
    call("POST", "/api/events/measurements", {
        "event_id": "dm1", "cargo_no": "DEMO-1",
        "occurred_at": "2026-01-01T01:00:00Z", "temperature": "6.0",
    })

    # 3. 交接：03:00 A -> B
    call("POST", "/api/events/handovers", {
        "event_id": "dh1", "cargo_no": "DEMO-1",
        "occurred_at": "2026-01-01T03:00:00Z",
        "from_party": "WAREHOUSE-A", "to_party": "CARRIER-B",
    })

    # 4. 测温：04:00 不超温关闭区间
    call("POST", "/api/events/measurements", {
        "event_id": "dm2", "cargo_no": "DEMO-1",
        "occurred_at": "2026-01-01T04:00:00Z", "temperature": "4.0",
    })

    # 5. 同内容重传 -> 200, deduplicated=true
    call("POST", "/api/events/measurements", {
        "event_id": "dm1", "cargo_no": "DEMO-1",
        "occurred_at": "2026-01-01T01:00:00Z", "temperature": "6.0",
    })

    # 6. 非法交接（交出方不是当时保管方）-> 409 整体拒绝
    call("POST", "/api/events/handovers", {
        "event_id": "dh-bad", "cargo_no": "DEMO-1",
        "occurred_at": "2026-01-01T05:00:00Z",
        "from_party": "OTHER", "to_party": "CARRIER-C",
    })

    # 7. 查询越温责任：A 担 [01:00,03:00)，B 担 [03:00,04:00)
    call("GET", "/api/cargo/DEMO-1/violations")


if __name__ == "__main__":
    main()
