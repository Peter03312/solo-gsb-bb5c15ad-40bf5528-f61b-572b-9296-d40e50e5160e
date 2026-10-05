#!/usr/bin/env bash
# 一次性开场码调用示例。启动服务后执行：bash examples/demo_opening_codes.sh
#
# 脚本会把演示场次安排在 START_DELAY 秒后开场（默认 30 秒），以便在真实
# 墙钟下演示：领码 -> 过早核销被拒 -> 开场内核销 -> 重试返回首次回执。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
START_DELAY="${START_DELAY:-30}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }
# 优先用 jq 提取开场码；无 jq 时回退到 python3
extract_code() {
  if command -v jq >/dev/null 2>&1; then jq -r '.code';
  else python3 -c 'import sys,json;print(json.load(sys.stdin)["code"])'; fi
}
# 计算“当前 + N 秒”的 UTC ISO 时间（GNU date；macOS 可用 gdate 或自行设置 START_AT）
START_AT="${START_AT:-$(date -u -d "${START_DELAY} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v+"${START_DELAY}"S +%Y-%m-%dT%H:%M:%SZ)}"

FILM="film-opening-demo"
KEY="opening-demo-1"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入授权（hall-1，宽有效时段，上限 3） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2040-01-01T00:00:00Z\",
  \"max_sessions\": 3, \"note\": \"开场码演示授权\"
}" | pretty

echo
echo "== 2. 核验核准一个场次（计划开场：$START_AT，${START_DELAY}s 后） =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"$START_AT\", \"idempotency_key\": \"$KEY\"
}" | pretty

echo
echo "== 3. 运营方首次领取开场码：原码仅此一次返回（code 字段） =="
CLAIM_RESP="$(curl -s -X POST "$BASE/verifications/$KEY/opening-code")"
echo "$CLAIM_RESP" | pretty
CODE="$(echo "$CLAIM_RESP" | extract_code)"
echo "拿到开场码：$CODE"

echo
echo "== 4. 重复领取：只回状态（status=issued, replay=true, code=null），不泄露原码 =="
curl -s -X POST "$BASE/verifications/$KEY/opening-code" | pretty
echo "-- GET 查询同样只见状态、不见原码 --"
curl -s "$BASE/verifications/$KEY/opening-code" | pretty

echo
echo "== 5. 过早核销（计划开场前）：422 REDEMPTION_NOT_OPEN，拒绝且无回执 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE\", \"venue_id\": \"hall-1\"}"

echo
echo "== 6. 场馆不符：422 VENUE_MISMATCH（等开场后演示，窗口为开场起 30 分钟） =="
echo "等待 ${START_DELAY}s 到达计划开场时刻……"
sleep "$((START_DELAY + 1))"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE\", \"venue_id\": \"hall-9\"}"

echo
echo "== 7. 场馆凭码在窗口内核销：返回场次、实际核销时间与关联授权 =="
curl -s -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE\", \"venue_id\": \"hall-1\"}" | pretty

echo
echo "== 8. 同一码重试（换场馆、甚至过窗口后）：始终返回首次回执 replay=true，不重复记场次 =="
curl -s -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE\", \"venue_id\": \"hall-9\"}" | pretty

echo
echo "== 9. 状态已变为 redeemed；原核验结果与额度占用不因核销变化 =="
curl -s "$BASE/verifications/$KEY/opening-code" | pretty
curl -s "$BASE/verifications/$KEY" | pretty
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 10. 未知码：404 NOT_FOUND（不产生回执） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d '{"code": "OC-does-not-exist", "venue_id": "hall-1"}'

echo
echo "== 11. 取消即作废：领码后取消，码立即失效（status=void，核销 422） =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-12-31T19:30:00Z\", \"idempotency_key\": \"opening-demo-2\"
}" >/dev/null
CODE2="$(curl -s -X POST "$BASE/verifications/opening-demo-2/opening-code" | extract_code)"
echo "第二场开场码：$CODE2"
curl -s -X POST "$BASE/verifications/opening-demo-2/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "片方撤场，场次取消"}' | pretty
curl -s "$BASE/verifications/opening-demo-2/opening-code" | pretty
echo "-- 用已作废的码核销：422 SCREENING_CANCELLED --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/opening-codes/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE2\", \"venue_id\": \"hall-1\"}"

echo
echo "== 12. 过期窗口（开场 +30 分钟之后）：422 REDEMPTION_WINDOW_CLOSED =="
echo "（窗口边界用测试覆盖；此处演示用一场 2020 年的旧核准无法再领码——）"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2020-01-01T10:00:00Z\", \"idempotency_key\": \"opening-demo-old\"
}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/opening-demo-old/opening-code"
