#!/usr/bin/env bash
# T0：便携版 PostgreSQL 初始化（Git Bash 执行）
# 用法：
#   bash backend/scripts/setup_pg.sh init    # 首次：initdb + 建库 + 启动
#   bash backend/scripts/setup_pg.sh start   # 启动
#   bash backend/scripts/setup_pg.sh stop    # 停止
set -euo pipefail

PG_HOME="/c/tools/pg15/pgsql"          # 解压后的 binaries 根
PG_DATA="/c/tools/pg15/data"           # 数据目录
PG_PORT=5432
PG_USER=prof_graph
PG_PASS=prof_graph_dev

cmd="${1:-init}"

if [ ! -d "$PG_HOME" ]; then
  echo "错误：未找到 $PG_HOME，请先解压便携包到 C:\\tools\\pg15" >&2
  exit 1
fi

case "$cmd" in
  init)
    if [ ! -d "$PG_DATA" ]; then
      echo ">> initdb..."
      mkdir -p "$PG_DATA"
      echo "$PG_PASS" > /c/tools/pg15/pwfile
      "$PG_HOME/bin/initdb.exe" -D "$(cygpath -w "$PG_DATA")" -U "$PG_USER" \
        -A md5 --pwfile="$(cygpath -w /c/tools/pg15/pwfile)" -E UTF8 >/dev/null
      rm -f /c/tools/pg15/pwfile
    else
      echo ">> 数据目录已存在，跳过 initdb"
    fi
    ;;
esac

running() { "$PG_HOME/bin/pg_ctl.exe" -D "$(cygpath -w "$PG_DATA")" status >/dev/null 2>&1; }

case "$cmd" in
  init|start)
    if running; then echo ">> PG 已在运行"; else
      echo ">> 启动 PG（端口 $PG_PORT）..."
      "$PG_HOME/bin/pg_ctl.exe" -D "$(cygpath -w "$PG_DATA")" -l "$(cygpath -w /c/tools/pg15/pg.log)" start
    fi
    ;;
  stop)
    "$PG_HOME/bin/pg_ctl.exe" -D "$(cygpath -w "$PG_DATA")" stop -m fast
    ;;
esac

PSQL="$PG_HOME/bin/psql.exe"
CONNT="-h localhost -p $PG_PORT -U $PG_USER"

if [ "$cmd" = "init" ]; then
  echo ">> 创建数据库 prof_graph / prof_graph_test ..."
  for db in prof_graph prof_graph_test; do
    PGPASSWORD="$PG_PASS" "$PSQL" $CONNT -d postgres -tc \
      "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
      || PGPASSWORD="$PG_PASS" "$PSQL" $CONNT -d postgres -c "CREATE DATABASE \"$db\""
  done
  echo ">> 完成。连接串：postgresql://$PG_USER:$PG_PASS@localhost:$PG_PORT/prof_graph"
fi
