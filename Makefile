# ==============================================================================
# ObsAI 开发管理工具集 (Makefile)
# ==============================================================================
# 提供便捷指令以支持前后端统一开发、单服务启动、依赖安装、构建及测试。

.DEFAULT_GOAL := help

# 路径与环境变量定义
ROOT_DIR     := $(shell pwd)
PYTHON       := $(ROOT_DIR)/.venv/bin/python
PYTEST       := $(ROOT_DIR)/.venv/bin/pytest
WEB_DIR      := $(ROOT_DIR)/web

HOST         ?= 127.0.0.1
PORT         ?= 8000
WEB_PORT     ?= 5173

.PHONY: help dev run api backend web frontend build build-web test test-backend test-frontend install install-backend install-frontend lint clean smoke

help: ## 显示 Makefile 支持的所有命令与说明
	@echo "======================================================================"
	@echo "  ObsAI 项目开发管理指令 (Makefile)"
	@echo "======================================================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "常用示例:"
	@echo "  make dev          # 一键同时启动前后端服务（开发模式）"
	@echo "  make api          # 仅启动后端 FastAPI API 服务"
	@echo "  make web          # 仅启动前端 Vite 开发服务器"
	@echo "  make test         # 运行全部后端与前端测试"
	@echo "  make build        # 构建前端静态产物到 web/dist"
	@echo "======================================================================"

dev: ## 同时启动后端 FastAPI 与前端 Vite 开发服务器（推荐）
	@OBSAI_UI_HOST=$(HOST) OBSAI_UI_PORT=$(PORT) ./scripts/dev.sh

run: dev ## dev 的别名，启动前后端完整开发环境

api: ## 仅启动后端 FastAPI 服务（带热重载与本地代理绕过）
	@echo "启动后端 API -> http://$(HOST):$(PORT)"
	@NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1" \
		$(PYTHON) -m uvicorn obsai.api.app:app --host $(HOST) --port $(PORT) --reload

backend: api ## api 的别名

web: ## 仅启动前端 Vite 开发服务器
	@echo "启动前端 Web -> http://$(HOST):$(WEB_PORT)"
	@cd $(WEB_DIR) && npm run dev

frontend: web ## web 的别名

build: build-web ## build-web 的别名

build-web: ## 构建前端静态生产产物到 web/dist
	@./scripts/build-web.sh

install: install-backend install-frontend ## 安装后端（Python/uv）与前端（Node/npm）全部依赖

install-backend: ## 安装后端 Python 依赖
	@if command -v uv >/dev/null 2>&1; then \
		uv sync; \
	else \
		echo "未检测到 uv 命令，请确保已安装 uv 或激活对应 Python 虚拟环境"; \
	fi

install-frontend: ## 安装前端 Node.js 依赖
	@cd $(WEB_DIR) && npm install

test: test-backend test-frontend ## 运行后端与前端全部单元测试

test-backend: ## 运行后端 Python 单元测试与集成测试
	@$(PYTEST) tests/unit/

test-frontend: ## 运行前端 Vitest 单元测试
	@cd $(WEB_DIR) && npm test

lint: ## 运行前端静态检查 (oxlint)
	@cd $(WEB_DIR) && npm run lint

smoke: ## 运行前端端到端无头 Chromium 冒烟测试
	@$(PYTHON) scripts/web-smoke.py

clean: ## 清理构建产物与临时缓存
	@rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules/.vite
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "清理完成"

