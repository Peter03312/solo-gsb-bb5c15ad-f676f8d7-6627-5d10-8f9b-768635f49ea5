#!/usr/bin/env bash
# 冷链追责服务端到端调用示例（curl）。
# 用法：
#   ./examples/api_examples.sh                 # 默认 http://localhost:8000
#   BASE_URL=http://localhost:18000 ./examples/api_examples.sh
#
# 脚本演示：建货、测温/交接、跨交接拆分责任、晚到记录重算、
# 幂等重传、异内容拒绝、越权交接拒绝、同时刻冲突、早于起始时间拒绝、
# 未闭合区间标记。
set -u
BASE_URL="${BASE_URL:-http://localhost:8000}"

call() {
  local title="$1"; shift
  echo
  echo "── $title"
  echo "  $*"
  # 输出 HTTP 状态码与响应体
  curl -sS -w '\n  [HTTP %{http_code}]\n' "$@"
}

if ! command -v curl >/dev/null; then
  echo "需要 curl"; exit 1
fi

echo "=========================================================="
echo "货物 C-001：晚到正常测温重算责任 + 各类拒绝/幂等"
echo "=========================================================="

call "1) 创建货物（编号/起始时间/温度上限/起始保管方）" \
  -X POST "$BASE_URL/api/cargo" -H 'Content-Type: application/json' \
  -d '{"cargo_no":"C-001","start_time":"2026-01-01T00:00:00Z","temp_upper_limit":"5.0","initial_holder":"WAREHOUSE-A"}'

call "2) 01:00 测温 6.0℃，超过上限 5.0 → 开启越温区间" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-001","cargo_no":"C-001","occurred_at":"2026-01-01T01:00:00Z","temperature":"6.0"}'

call "3) 02:00 测温 7.2℃，区间保持开启" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-002","cargo_no":"C-001","occurred_at":"2026-01-01T02:00:00Z","temperature":"7.2"}'

call "4) 03:00 交接 WAREHOUSE-A → CARRIER-B（交出方=当时保管方，接受）" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"h-001","cargo_no":"C-001","occurred_at":"2026-01-01T03:00:00Z","from_party":"WAREHOUSE-A","to_party":"CARRIER-B"}'

call "5) 04:00 测温 4.5℃，首次不超温 → 关闭区间；跨交接拆分责任" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-003","cargo_no":"C-001","occurred_at":"2026-01-01T04:00:00Z","temperature":"4.5"}'

call "6) 查询越温报告：A 担 [01:00,03:00)，B 担 [03:00,04:00)" \
  "$BASE_URL/api/cargo/C-001/violations"

call "7) 晚到：补录 02:30 测温 4.0℃（不超温）→ 受影响区间重算，区间改在 02:30 关闭" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-late","cargo_no":"C-001","occurred_at":"2026-01-01T02:30:00Z","temperature":"4.0"}'

call "8) 重算后报告：越温区间 [01:00,02:30)，全部责任在 WAREHOUSE-A" \
  "$BASE_URL/api/cargo/C-001/violations"

call "9) 非法交接：交出方 STRANGER-X 不是当时保管方 CARRIER-B → 409 整体拒绝" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"h-bad","cargo_no":"C-001","occurred_at":"2026-01-01T05:00:00Z","from_party":"STRANGER-X","to_party":"CARRIER-C"}'

call "10) 合法交接 CARRIER-B → CARRIER-C" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"h-002","cargo_no":"C-001","occurred_at":"2026-01-01T05:00:00Z","from_party":"CARRIER-B","to_party":"CARRIER-C"}'

call "11) 同ID异内容重传（h-002 改成 CARRIER-D）→ 409 拒绝" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"h-002","cargo_no":"C-001","occurred_at":"2026-01-01T05:00:00Z","from_party":"CARRIER-B","to_party":"CARRIER-D"}'

call "12) 同ID同内容重传 h-002 → 200 deduplicated=true，不重复写入" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"h-002","cargo_no":"C-001","occurred_at":"2026-01-01T05:00:00Z","from_party":"CARRIER-B","to_party":"CARRIER-C"}'

call "13) 测温 m-001 同内容重传 → 200 deduplicated=true" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-001","cargo_no":"C-001","occurred_at":"2026-01-01T01:00:00Z","temperature":"6.0"}'

call "14) 事件早于货物起始时间 → 422 拒绝" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-before","cargo_no":"C-001","occurred_at":"2025-12-31T23:00:00Z","temperature":"9.0"}'

call "15) 同一时刻 01:00 再报一笔测温 → 409 冲突拒绝" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"m-conflict","cargo_no":"C-001","occurred_at":"2026-01-01T01:00:00Z","temperature":"6.5"}'

echo
echo "=========================================================="
echo "货物 C-002：未闭合区间 + 跨交接拆分开放区间"
echo "=========================================================="

call "16) 创建冷冻货，上限 -18℃，起始保管方 WAREHOUSE-1" \
  -X POST "$BASE_URL/api/cargo" -H 'Content-Type: application/json' \
  -d '{"cargo_no":"C-002","start_time":"2026-01-01T00:00:00Z","temp_upper_limit":"-18.0","initial_holder":"WAREHOUSE-1"}'

call "17) 01:00 -15℃ 开启越温，02:00 -20℃ 关闭 → WAREHOUSE-1 担 [01:00,02:00)" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"cm-1","cargo_no":"C-002","occurred_at":"2026-01-01T01:00:00Z","temperature":"-15.0"}'

call "  17b) 02:00 -20℃" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"cm-2","cargo_no":"C-002","occurred_at":"2026-01-01T02:00:00Z","temperature":"-20.0"}'

call "18) 06:00 -10℃ 再次越温（区间将保持未闭合）" \
  -X POST "$BASE_URL/api/events/measurements" -H 'Content-Type: application/json' \
  -d '{"event_id":"cm-3","cargo_no":"C-002","occurred_at":"2026-01-01T06:00:00Z","temperature":"-10.0"}'

call "19) 07:00 交接 WAREHOUSE-1 → CARRIER-2，开放区间在交接时刻拆分" \
  -X POST "$BASE_URL/api/events/handovers" -H 'Content-Type: application/json' \
  -d '{"event_id":"ch-1","cargo_no":"C-002","occurred_at":"2026-01-01T07:00:00Z","from_party":"WAREHOUSE-1","to_party":"CARRIER-2"}'

call "20) 最终报告：第二个区间 end=null（未结束），尾片段归 CARRIER-2" \
  "$BASE_URL/api/cargo/C-002/violations"

call "21) 列出 C-001 全部事件（按发生时间）" \
  "$BASE_URL/api/events?cargo_no=C-001"

echo
echo "完成。交互文档：$BASE_URL/docs"
