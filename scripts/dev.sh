#!/usr/bin/env bash
# 本地开发：并行启动 FastAPI（API 层）与 Vite（前端开发服务器）。
# 用法：./scripts/dev.sh
# 环境变量：OBSAI_UI_HOST（默认 127.0.0.1）、OBSAI_UI_PORT（默认 8000）、OBSAI_UI_NO_RELOAD=1 关闭热重载
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf '未找到虚拟环境：%s\n请先运行 uv sync\n' "$PYTHON" >&2
    exit 1
fi

HOST="${OBSAI_UI_HOST:-127.0.0.1}"
PORT="${OBSAI_UI_PORT:-8000}"

# IDE 会注入 HTTP_PROXY/HTTPS_PROXY 却不带 NO_PROXY，而 httpx 的 trust_env 会把
# 指向 localhost 的请求也塞进代理，导致 embedding 调用报
# RemoteProtocolError: Server disconnected（详见 .workbuddy-ai/docs/ollama-local-setup.md）。
# 这里保证 localhost 始终直连，同时保留用户已有的其它条目。
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1"
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1"

RELOAD_ARGS=(--reload)
if [ "${OBSAI_UI_NO_RELOAD:-0}" = "1" ]; then
    RELOAD_ARGS=()
fi
API_PID=""
WEB_PID=""

cleanup() {
    trap - EXIT INT TERM
    for pid in "$WEB_PID" "$API_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

printf '启动 API   -> http://%s:%s\n' "$HOST" "$PORT"
# `${arr[@]+"${arr[@]}"}` 是 bash 3.2 下展开空数组的必需写法：
# 直接写 "${RELOAD_ARGS[@]}" 在 set -u 下会报 unbound variable（macOS 自带 bash 3.2）。
"$PYTHON" -m uvicorn obsai.api.app:app \
    --host "$HOST" --port "$PORT" ${RELOAD_ARGS[@]+"${RELOAD_ARGS[@]}"} &
API_PID=$!

printf '启动前端 -> http://127.0.0.1:5173\n'
(cd "$ROOT/web" && npm run dev) &
WEB_PID=$!

# macOS 自带 bash 3.2 没有 `wait -n`，用轮询替代：任一进程退出即收尾，
# 剩下的交给 cleanup trap 处理，避免一个服务挂掉后脚本还挂着。
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do
    sleep 1
done
