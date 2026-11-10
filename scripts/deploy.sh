#!/usr/bin/env bash
# 服务器端发布脚本：拉取最新代码、同步依赖并重启服务。
# 用法（在服务器仓库目录内）：./scripts/deploy.sh
# 首次部署请见 README「服务器部署」章节，本脚本只负责日常更新。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> git pull"
git pull --ff-only

echo "==> uv sync"
uv sync --locked

echo "==> systemctl restart mockinterview"
sudo systemctl restart mockinterview
sleep 1

echo "==> service status"
sudo systemctl is-active mockinterview
sudo systemctl status mockinterview --no-pager -l | head -12
echo "==> 完成。本机自检：curl -s http://127.0.0.1:8000/api/topics | head -c 200"
