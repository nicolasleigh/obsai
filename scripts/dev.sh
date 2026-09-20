#!/usr/bin/env bash
# 本地开发：并行启动 FastAPI（API 层）与 Vite（前端开发服务器）。
# 用法：./scripts/dev.sh
# 环境变量：OBSAI_UI_HOST（默认 127.0.0.1）、OBSAI_UI_PORT（默认 8000）、OBSAI_UI_NO_RELOAD=1 关闭热重载、OBSAI_UI_NO_WEB=1 仅启动 API、OBSAI_ENV_FILE（自定义 .env 路径）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf '未找到虚拟环境：%s\n请先运行 uv sync\n' "$PYTHON" >&2
    exit 1
fi

# 自动加载项目根目录的 .env。通过 python-dotenv 启动整个开发脚本，确保
# API（以及未来可能读取环境变量的前端进程）都能继承这些变量。命令行中
# 已经 export 的变量优先于 .env，避免本地临时覆盖被配置文件意外替换。
ENV_FILE="${OBSAI_ENV_FILE:-$ROOT/.env}"
if [ -f "$ENV_FILE" ] && [ "${OBS_AI_DOTENV_LOADED:-0}" != "1" ]; then
    # Keep the recursion guard outside the OBSAI_ namespace so Pydantic Settings
    # does not mistake this internal marker for an application configuration key.
    export OBS_AI_DOTENV_LOADED=1
    printf '加载环境变量 -> %s\n' "$ENV_FILE"
    exec "$PYTHON" -m dotenv -f "$ENV_FILE" run --no-override -- "$0" "$@"
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

# ``uvicorn --reload`` and ``npm run dev`` are supervisors: each one owns a
# child process that is the actual listener. Killing only the PID returned by
# ``$!`` leaves that child behind, so the next start reports "address already in
# use" and Ctrl-C appears not to work. Walk the descendants before signalling
# the root so the cleanup also works on macOS, where ``setsid`` is not available
# by default and a process-group kill could take this shell down with it.
descendants_of() {
    local pid="$1"
    local child
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        descendants_of "$child"
        printf '%s\n' "$child"
    done
}

terminate_tree() {
    local root="$1"
    local descendants pid alive attempt
    descendants="$(descendants_of "$root")"

    # Signal children first so they do not become orphans when the supervisor
    # exits. Keep the original PID list: once a parent exits, pgrep -P cannot
    # discover its former children anymore.
    for pid in $descendants; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    kill -TERM "$root" 2>/dev/null || true

    # Give cooperative shutdown (including transaction rollback and journal
    # writes) a short grace period. The fallback is limited to the processes we
    # started, so it cannot terminate an unrelated Python process on the host.
    for attempt in 1 2 3 4 5; do
        alive=0
        for pid in $descendants "$root"; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=1
                break
            fi
        done
        [ "$alive" -eq 0 ] && return
        sleep 1
    done

    for pid in $descendants "$root"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
}

cleanup() {
    trap - EXIT INT TERM
    for pid in "$WEB_PID" "$API_PID"; do
        if [ -n "$pid" ]; then
            terminate_tree "$pid"
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

if [ "${OBSAI_UI_NO_WEB:-0}" != "1" ]; then
    printf '启动前端 -> http://127.0.0.1:5173\n'
    (cd "$ROOT/web" && npm run dev) &
    WEB_PID=$!
fi

# macOS 自带 bash 3.2 没有 `wait -n`，用轮询替代：任一进程退出即收尾，
# 剩下的交给 cleanup trap 处理，避免一个服务挂掉后脚本还挂着。
EXIT_STATUS=0
if [ -z "$WEB_PID" ]; then
    wait "$API_PID" 2>/dev/null || EXIT_STATUS=$?
else
    while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do
        sleep 1
    done
    if ! kill -0 "$API_PID" 2>/dev/null; then
        wait "$API_PID" 2>/dev/null || EXIT_STATUS=$?
    else
        # The web process exited first; report a failed dev session instead of
        # silently leaving the API running after cleanup.
        EXIT_STATUS=1
    fi
fi
exit "$EXIT_STATUS"
