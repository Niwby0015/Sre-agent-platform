#!/usr/bin/env bash
# Start the SRE agent API server (FastAPI + web UI).
# Port comes from API_PORT in .env (currently 8090 — 8080 is taken on this host).
set -euo pipefail
cd "$(dirname "$0")"

# 直连外网：清掉 shell 里的代理变量。mihomo 代理 (127.0.0.1:7890) 已失效，
# 且 anthropic SDK 检测到 SOCKS 代理会因缺少 socksio 包直接崩溃。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

exec .venv/bin/python api.py
