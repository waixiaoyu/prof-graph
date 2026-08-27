#!/usr/bin/env bash
# 一键启动全栈（M1 做实 2026-08-26；M2 无新增进程，调度链扩展见 README）：
# PostgreSQL → 后端 8000（APScheduler 内置 M1+M2 全部定时任务）→ 前端 5173。
# 凌晨自动链：02:00 备份 → 03:00 arXiv 管线 → 04:00 资讯(scope=news)
#            → 05:00 官网爬取(scope=crawl)，各链后跑 C1-C10 巡检。
# 机器重启或进程被回收后，在仓库任意位置执行：bash scripts/start_all.sh
# 已在运行的组件自动跳过；失败会指出对应日志文件。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PG_CTL="C:\\tools\\pg15\\pgsql\\bin\\pg_ctl.exe"
PG_DATA="C:\\tools\\pg15\\data"
PG_LOG="C:\\tools\\pg15\\pg.log"

port_up() { netstat -ano | grep -q ":$1 .*LISTEN"; }

wait_port() {  # $1=端口 $2=超时秒
  local port=$1 timeout=${2:-30} i=0
  until port_up "$port"; do
    sleep 1; i=$((i+1))
    [ "$i" -ge "$timeout" ] && return 1
  done
  return 0
}

echo "== 1/3 PostgreSQL (5432) =="
if port_up 5432; then
  echo "  已在运行，跳过"
else
  "$PG_CTL" -D "$PG_DATA" -l "$PG_LOG" start || { echo "  PG 启动失败，看 $PG_LOG"; exit 1; }
  wait_port 5432 30 && echo "  就绪" || { echo "  30 秒仍未监听 5432，看 $PG_LOG"; exit 1; }
fi

echo "== 2/3 后端 uvicorn (:8000) =="
if port_up 8000; then
  echo "  已在运行，跳过"
else
  (cd "$ROOT/backend" && nohup ./.venv/Scripts/python.exe -m uvicorn app.main:app \
     --host 127.0.0.1 --port 8000 > uvicorn.log 2>&1 &)
  wait_port 8000 30 && echo "  就绪" || { echo "  30 秒仍未监听 8000，看 backend/uvicorn.log"; exit 1; }
fi

echo "== 3/3 前端 vite (:5173) =="
if port_up 5173; then
  echo "  已在运行，跳过"
else
  (cd "$ROOT/frontend" && nohup npm run dev > vite.log 2>&1 &)
  wait_port 5173 30 && echo "  就绪" || { echo "  30 秒仍未监听 5173，看 frontend/vite.log"; exit 1; }
fi

echo "== 全栈就绪 =="
echo "  前端界面   http://127.0.0.1:5173"
echo "  后端文档   http://127.0.0.1:8000/docs"
echo "  数据巡检   http://127.0.0.1:8000/api/admin/integrity"
