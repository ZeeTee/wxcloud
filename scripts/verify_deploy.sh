#!/usr/bin/env bash
#
# 部署验收脚本 —— AI 大模型行业日报归档服务
#
# 用法:
#   BASE_URL=https://<你的云托管域名> bash scripts/verify_deploy.sh --read-only
#   BASE_URL=https://<你的云托管域名> REPORT_API_KEY=<密钥> bash scripts/verify_deploy.sh
#
# ⚠️ 写接口的测试**必须在出口 IP 39.96.95.216 上跑**(那台跑日报的机器),
#    否则来源 IP 不在白名单,会得到 403 —— 那是脚本跑错地方,不是服务有问题。
#    只读部分(--read-only)不受此限,在哪跑都行。
#
# 依赖:curl、python3(解析 JSON)。不需要 jq。

set -uo pipefail

BASE_URL="${BASE_URL:-}"
REPORT_API_KEY="${REPORT_API_KEY:-}"
DATE="${DATE:-2026-09-22}"
EXPECTED_EGRESS_IP="${EXPECTED_EGRESS_IP:-39.96.95.216}"

READ_ONLY=0
[[ "${1:-}" == "--read-only" ]] && READ_ONLY=1
[[ -n "$REPORT_API_KEY" ]] || READ_ONLY=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
FIXTURE="$REPO_DIR/wxcloudrun/tests/fixtures/daily-${DATE}.html"

pass=0; fail=0; warned=0; skipped=0
ok()      { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()     { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
warn()    { printf '  \033[33m!\033[0m %s\n' "$1"; warned=$((warned+1)); }
skip()    { printf '  \033[90m–\033[0m %s\n' "$1"; skipped=$((skipped+1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [[ -z "$BASE_URL" ]]; then
  echo "用法:BASE_URL=https://<云托管域名> [REPORT_API_KEY=<密钥>] bash $0 [--read-only]" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"

BODY_FILE="$(mktemp)"; trap 'rm -f "$BODY_FILE"' EXIT
req()  { curl -sS -o "$BODY_FILE" -w '%{http_code}' --max-time 20 "$@" 2>/dev/null || echo "000"; }
body() { cat "$BODY_FILE"; }
jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}))" "$1" 2>/dev/null; }

# expect <期望状态码> <说明> <curl 参数...>
expect() {
  local want="$1" label="$2"; shift 2
  local code; code="$(req "$@")"
  if [[ "$code" == "$want" ]]; then
    ok "$label → $code"
  else
    bad "$label → $code(应为 $want)$(body | head -c 100)"
  fi
}

echo "目标:$BASE_URL"
echo "模式:$([[ $READ_ONLY == 1 ]] && echo '只读(未提供密钥)' || echo '完整(含写入)')"

# ---------------------------------------------------------------- 1. 存活探针
section "1. 存活探针"
code="$(req "$BASE_URL/healthz")"
if [[ "$code" == "200" && "$(body)" == "ok" ]]; then
  ok "/healthz → 200 ok"
else
  bad "/healthz → $code $(body | head -c 80)"
fi

# ---------------------------------------------------------------- 2. 首页
section "2. 首页(服务说明页)"
code="$(req "$BASE_URL/")"
if [[ "$code" == "200" ]] && grep -q "AI 大模型行业日报" "$BODY_FILE"; then
  ok "/ → 200,内容正常"
else
  bad "/ → $code(内容不含预期文字)"
fi
if body | grep -qiE 'traceback|DisallowedHost|Counters|计数器'; then
  bad "首页疑似泄露调试信息或残留 demo(检查 DJANGO_DEBUG 是否为 0)"
else
  ok "首页无调试信息泄露"
fi

# ---------------------------------------------------------------- 3. 响应安全头
section "3. 响应安全头"
HDRS="$(curl -sSI --max-time 20 "$BASE_URL/" 2>/dev/null | tr -d '\r')"
for h in Content-Security-Policy X-Content-Type-Options Referrer-Policy X-Frame-Options; do
  if grep -qi "^$h:" <<<"$HDRS"; then ok "有 $h"; else bad "缺少 $h"; fi
done

# ---------------------------------------------------------------- 4. 错误路径
section "4. 错误路径"
code="$(req "$BASE_URL/daily/2099-01-01")"
if [[ "$code" == "404" ]] && grep -q "没有找到这一天的日报" "$BODY_FILE"; then
  ok "/daily/2099-01-01 → 404 友好页面"
else
  bad "/daily/2099-01-01 → $code(应为 404 友好页)"
fi

code="$(req "$BASE_URL/daily/not-a-date")"
[[ "$code" == "404" ]] && ok "/daily/not-a-date → 404" || bad "/daily/not-a-date → $code"

code="$(req "$BASE_URL/api/nope")"
if [[ "$code" == "404" ]] && body | grep -q '"code": *"not_found"'; then
  ok "/api/nope → 404 JSON 信封"
else
  bad "/api/nope → $code(应为 JSON 信封)"
fi

code="$(req "$BASE_URL/nope")"
[[ "$code" == "404" ]] && ok "/nope → 404" || bad "/nope → $code"

# ---------------------------------------------------------------- 5. 鉴权(不需要密钥即可验)
section "5. 鉴权:不需要正确密钥的部分"
expect 405 "GET /api/report(方法不对)" "$BASE_URL/api/report"
expect 401 "POST 无密钥" -X POST -H 'Content-Type: application/json' \
  --data '{"date":"2026-09-22","html":"<!DOCTYPE html><html><body>x</body></html>"}' \
  "$BASE_URL/api/report"
expect 401 "POST 密钥错误" -X POST -H 'Content-Type: application/json' -H 'X-Api-Key: definitely-wrong' \
  --data '{"date":"2026-09-22","html":"<!DOCTYPE html><html><body>x</body></html>"}' \
  "$BASE_URL/api/report"

# 准备真实载荷(两种模式都用得到)
if [[ -f "$FIXTURE" ]]; then
  PAYLOAD="$(python3 - "$FIXTURE" "$DATE" <<'PY'
import json, pathlib, sys
html = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
print(json.dumps({"date": sys.argv[2], "html": html,
                  "title": "AI大模型行业日报 · " + sys.argv[2]}, ensure_ascii=False))
PY
)"
else
  PAYLOAD='{"date":"'"$DATE"'","html":"<!DOCTYPE html><html><body><p>t</p></body></html>"}'
fi

if [[ $READ_ONLY == 1 ]]; then
  section "6. 内容校验 / 写入 / 读回 —— 已跳过"
  skip "POST 含 <script> → 422"
  skip "POST 非法 JSON → 400"
  skip "POST 非法日期 → 400"
  skip "POST 正常上报 → 201"
  skip "重复上报幂等 → 200 unchanged"
  skip "读回并逐字节对拍 ★核心验收"
  echo
  echo "  这几项都排在鉴权**之后**,没有正确密钥根本到不了 —— 所以只读模式验不了。"
  echo "  在出口 IP $EXPECTED_EGRESS_IP 上带 REPORT_API_KEY 跑一次即可全部覆盖。"

else

  section "6. 内容校验(需要先过鉴权)"
  expect 422 "POST 含 <script>" -X POST -H 'Content-Type: application/json' \
    -H "X-Api-Key: $REPORT_API_KEY" \
    --data '{"date":"'"$DATE"'","html":"<!DOCTYPE html><html><body><script>x</script></body></html>"}' \
    "$BASE_URL/api/report"
  expect 422 "POST 含事件属性" -X POST -H 'Content-Type: application/json' \
    -H "X-Api-Key: $REPORT_API_KEY" \
    --data '{"date":"'"$DATE"'","html":"<!DOCTYPE html><html><body><p onclick=\"x()\">a</p></body></html>"}' \
    "$BASE_URL/api/report"
  expect 422 "POST 无 DOCTYPE" -X POST -H 'Content-Type: application/json' \
    -H "X-Api-Key: $REPORT_API_KEY" \
    --data '{"date":"'"$DATE"'","html":"<html><body>x</body></html>"}' \
    "$BASE_URL/api/report"
  expect 400 "POST 非法 JSON" -X POST -H 'Content-Type: application/json' \
    -H "X-Api-Key: $REPORT_API_KEY" --data 'not-json' "$BASE_URL/api/report"
  expect 400 "POST 非法日期" -X POST -H 'Content-Type: application/json' \
    -H "X-Api-Key: $REPORT_API_KEY" \
    --data '{"date":"2026-13-45","html":"<!DOCTYPE html><html><body>x</body></html>"}' \
    "$BASE_URL/api/report"

  # ------------------------------------------------------------ 7. 写入
  section "7. 写入"
  code="$(req -X POST -H 'Content-Type: application/json' -H "X-Api-Key: $REPORT_API_KEY" \
          --data "$PAYLOAD" "$BASE_URL/api/report")"

  if [[ "$code" == "201" || "$code" == "200" ]]; then
    ok "POST /api/report → $code"
  elif [[ "$code" == "403" ]]; then
    bad "POST → 403 来源 IP 不在白名单"
    echo "     当前出口 IP:$(curl -sS --max-time 8 https://api.ipify.org 2>/dev/null || echo '查不到')"
    echo "     ⚠️ 这个脚本必须在 $EXPECTED_EGRESS_IP 上跑"
  elif [[ "$code" == "503" ]]; then
    bad "POST → 503 服务端没配环境变量:$(body | head -c 160)"
  else
    bad "POST /api/report → $code $(body | head -c 200)"
  fi

  if body | grep -q '"ok": *true'; then
    URL="$(body | jget "d['data']['url']")"
    ok "返回 url = $URL"
    [[ "$URL" == *"/daily/$DATE" ]] || bad "url 不是 /daily/$DATE —— 检查 REPORT_PUBLIC_BASE_URL"
    [[ "$(body | jget "d['data']['unchanged']")" == "True" ]] && \
      warn "本次 unchanged(这份内容之前已入库,正常)"
  fi

  code2="$(req -X POST -H 'Content-Type: application/json' -H "X-Api-Key: $REPORT_API_KEY" \
           --data "$PAYLOAD" "$BASE_URL/api/report")"
  if [[ "$code2" == "200" ]] && body | grep -q '"unchanged": *true'; then
    ok "重复上报 → 200 unchanged(幂等)"
  else
    bad "重复上报 → $code2 $(body | head -c 150)"
  fi

  # ------------------------------------------------------------ 8. 读回对拍
  section "8. 读回并逐字节对拍 ★核心验收"
  code="$(req "$BASE_URL/daily/$DATE")"
  [[ "$code" == "200" ]] && ok "GET /daily/$DATE → 200" || bad "GET /daily/$DATE → $code"

  CT="$(curl -sSI --max-time 20 "$BASE_URL/daily/$DATE" | tr -d '\r' | grep -i '^content-type:' | head -1)"
  if grep -qi 'text/html; charset=utf-8' <<<"$CT"; then
    ok "Content-Type: text/html; charset=utf-8"
  else
    bad "Content-Type 异常:$CT"
  fi

  if [[ -f "$FIXTURE" ]]; then
    got="$(sha256sum "$BODY_FILE" | cut -d' ' -f1)"
    want="$(sha256sum "$FIXTURE" | cut -d' ' -f1)"
    if [[ "$got" == "$want" ]]; then
      ok "返回内容与上报内容 sha256 一致:$got"
    else
      bad "sha256 不一致!返回=$got 上报=$want"
    fi
    n=$(grep -o 'href="http' "$BODY_FILE" | wc -l | tr -d ' ')
    if [[ "$n" -gt 0 ]]; then ok "页面里有 $n 条原文链接(读者能点)"; else bad "页面里没有任何原文链接!"; fi
  else
    warn "找不到夹具 $FIXTURE,跳过逐字节对拍"
  fi
fi

# ---------------------------------------------------------------- 9. 人工项
section "9. 还需人工看一眼"
echo "  ① 到云托管控制台看服务日志,找这一行:"
echo "       POST /api/report candidates=[...] observed={...}"
echo "     确认平台实际把真实 IP 放在哪个转发头里,然后把环境变量"
echo "     REPORT_IP_HEADERS 设成那一个(例如 x-original-forwarded-for)。"
[[ $READ_ONLY == 1 ]] || echo "  ② 把上面那个 url 填进公众号草稿的「阅读原文」,手机上点开验一次。"
echo "  ③ 手机上打开详情页,逐条点新闻标题,确认能跳到原文。"

section "汇总"
echo "  通过 $pass / 失败 $fail / 提醒 $warned / 跳过 $skipped"
[[ $fail -eq 0 ]] && echo "  ✅ 部署验收通过" || echo "  ❌ 有 $fail 项未通过"
exit $(( fail > 0 ? 1 : 0 ))
