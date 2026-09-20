#!/usr/bin/env bash
# 构建前端静态资源到 web/dist，供 FastAPI 同源托管。
# 用法：./scripts/build-web.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/web"

if [ ! -d "$WEB/node_modules" ]; then
    printf '未安装前端依赖，先运行：cd web && npm install\n' >&2
    exit 1
fi

cd "$WEB"
npm run build

printf '\n构建完成：%s\n' "$WEB/dist"
