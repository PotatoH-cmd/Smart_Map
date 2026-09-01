#!/bin/bash
# backup_memory.sh — 记忆数据每日备份（sessions.db / LlamaIndex 向量库 / Kuzu 图谱）
# crontab: 30 2 * * * /home/szgczx/python/map_assistant_v1/backend/scripts/backup_memory.sh
set -u

BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ROOT="/home/szgczx/backups/memory"
TODAY="$(date +%Y%m%d)"
DEST="$BACKUP_ROOT/$TODAY"
KEEP=14

mkdir -p "$DEST"

# 1. SQLite 在线一致性备份（.backup 支持热备，不锁写）
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$BACKEND_DIR/sessions.db" ".backup '$DEST/sessions.db'"
else
    cp "$BACKEND_DIR/sessions.db" "$DEST/sessions.db"
fi

# 2. LlamaIndex 向量库 + Kuzu 图谱打包（kuzu_graph 可能是目录或单文件）
tar -czf "$DEST/llama_index_storage.tar.gz" -C "$BACKEND_DIR" llama_index_storage 2>/dev/null
if [ -e "$BACKEND_DIR/kuzu_graph" ]; then
    KUZU_ITEMS="kuzu_graph"
    [ -f "$BACKEND_DIR/kuzu_graph_meta.json" ] && KUZU_ITEMS="kuzu_graph kuzu_graph_meta.json"
    tar -czf "$DEST/kuzu_graph.tar.gz" -C "$BACKEND_DIR" $KUZU_ITEMS 2>/dev/null
fi

# 3. 轮转：仅保留最近 KEEP 份
ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf

echo "[$(date '+%F %T')] backup done -> $DEST ($(du -sh "$DEST" | cut -f1))"
