"""
workspace_state.py — 会话级工作区状态树（阶段3）。

对齐文章 GISStateTree / artifact lineage 理念：让 LLM 知道"当前工作区有什么、
产物从哪来"，使多轮任务（"刚才的结果""最终结果"）真正可引用，前端不再依赖
全量历史回传。

结构（按 session_id，持久化到 sessions.db 新表 workspace_states）：
  layers[]     — 图层名、来源（表/文件）、更新时间
  artifacts[]  — geojson / 报告 / 图表产物，含 source_run_id / source_tool（lineage）
  last_summary — 上一轮结论摘要（阶段4 历史压缩也写入此字段）

每次 run 终态由 RunEngine 回调 record() 增量更新；intent 分析通过
build_summary() 注入 ≤800 字状态摘要（阶段4 由 context_manager 统一预算）。
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .run_store import DEFAULT_DB_PATH, get_run_store

logger = logging.getLogger(__name__)

MAX_LAYERS = 20
MAX_ARTIFACTS = 50
SUMMARY_MAX_CHARS = 800
LAST_SUMMARY_MAX_CHARS = 300

# 图层相关的 map_command action
_LAYER_ACTIONS = ("load_vector_layer", "addGeoJsonLayer", "add_vector")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _empty_ws() -> Dict[str, Any]:
    return {
        "layers": [],
        "artifacts": [],
        "last_summary": "",
        "updated_at": "",
    }


class WorkspaceState:
    """会话状态树：内存缓存 + SQLite 持久化（复用 sessions.db）。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.ensure_table()

    # ------------------------------------------------------------------
    # 建表（不动既有表）
    # ------------------------------------------------------------------

    def ensure_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspace_states (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> Dict[str, Any]:
        if not session_id or session_id == "default":
            return _empty_ws()
        if session_id in self._cache:
            return self._cache[session_id]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state_json FROM workspace_states WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return _empty_ws()
        try:
            ws = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return _empty_ws()
        if not isinstance(ws, dict):
            return _empty_ws()
        self._cache[session_id] = ws
        return ws

    # ------------------------------------------------------------------
    # 记录（run 终态由 RunEngine 回调）
    # ------------------------------------------------------------------

    def record(
        self,
        session_id: str,
        run_id: str,
        tool_results: Optional[List[Dict]] = None,
        map_commands: Optional[List[Dict]] = None,
        report_url: Optional[str] = None,
        response: str = "",
    ) -> Dict[str, Any]:
        """增量更新会话状态树：从工具结果提取图层与产物（含 lineage）。"""
        if not session_id or session_id == "default":
            return _empty_ws()
        ws = self.get(session_id)
        now = _now_iso()

        # ── 1. 产物（artifacts）：报告 / GeoJSON / 图表 ──
        for item in (tool_results or []):
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            if tool_name in ("report_generator_tool", "caisha_report_tool") and result.get("success"):
                url = result.get("report_url") or result.get("download_url")
                if url:
                    self._append_artifact(ws, {
                        "type": "report",
                        "name": result.get("filename") or url.rsplit("/", 1)[-1],
                        "url": url,
                        "source_run_id": run_id,
                        "source_tool": tool_name,
                        "created_at": now,
                    })
            if tool_name == "qgis_mcp_tool" and result.get("combined_geojson"):
                url = result["combined_geojson"]
                self._append_artifact(ws, {
                    "type": "geojson",
                    "name": url.rsplit("/", 1)[-1],
                    "url": url,
                    "source_run_id": run_id,
                    "source_tool": tool_name,
                    "created_at": now,
                })
            if tool_name == "data_visualizer_tool" and result.get("success"):
                self._append_artifact(ws, {
                    "type": "chart",
                    "name": result.get("chart_type") or "chart",
                    "url": "",
                    "source_run_id": run_id,
                    "source_tool": tool_name,
                    "created_at": now,
                })
        if report_url:
            self._append_artifact(ws, {
                "type": "report",
                "name": report_url.rsplit("/", 1)[-1],
                "url": report_url,
                "source_run_id": run_id,
                "source_tool": "report_generator_tool",
                "created_at": now,
            })

        # ── 2. 图层（layers）：从 map_commands 与 GIS 结果提取 ──
        for mc in (map_commands or []):
            if not isinstance(mc, dict):
                continue
            action = mc.get("action", "")
            if action in _LAYER_ACTIONS:
                name = mc.get("table_name") or mc.get("url") or mc.get("name") or mc.get("layer", "")
                if not name:
                    continue
                source = "table" if mc.get("table_name") else "file"
                self._append_layer(ws, {"name": name, "source": source, "updated_at": now})
            elif action == "render":
                url = mc.get("geojson_url") or mc.get("url") or ""
                if url:
                    self._append_artifact(ws, {
                        "type": "geojson",
                        "name": url.rsplit("/", 1)[-1],
                        "url": url,
                        "source_run_id": run_id,
                        "source_tool": "map_tool",
                        "created_at": now,
                    })
        for item in (tool_results or []):
            if item.get("tool_name") == "qgis_mcp_tool":
                result = item.get("result", {})
                if isinstance(result, dict) and result.get("combined_geojson"):
                    self._append_layer(ws, {
                        "name": result["combined_geojson"].rsplit("/", 1)[-1],
                        "source": "geojson",
                        "updated_at": now,
                    })

        # ── 3. 上一轮结论摘要（截断） ──
        if response:
            text = str(response).strip().replace("\n", " ")
            ws["last_summary"] = (
                text[: LAST_SUMMARY_MAX_CHARS - 1] + "…"
                if len(text) > LAST_SUMMARY_MAX_CHARS else text
            )
        ws["updated_at"] = now

        # 限长防膨胀
        ws["layers"] = ws["layers"][-MAX_LAYERS:]
        ws["artifacts"] = ws["artifacts"][-MAX_ARTIFACTS:]

        self._save(session_id, ws)
        return ws

    # ------------------------------------------------------------------
    # 摘要（注入 LLM）
    # ------------------------------------------------------------------

    def set_last_summary(self, session_id: str, summary: str) -> None:
        """仅更新上轮结论摘要（阶段4 历史压缩滚动写入；不触碰图层/产物）。"""
        if not session_id or session_id == "default":
            return
        ws = self.get(session_id)
        ws["last_summary"] = (summary or "").strip()[: LAST_SUMMARY_MAX_CHARS]
        ws["updated_at"] = _now_iso()
        self._save(session_id, ws)

    def build_summary(self, session_id: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
        """生成 ≤max_chars 的状态摘要，供 intent 分析与 summarize 注入。"""
        ws = self.get(session_id)
        lines: List[str] = []
        layers = ws.get("layers") or []
        artifacts = ws.get("artifacts") or []
        if layers:
            lines.append("当前工作区图层：" + "、".join(
                f"{l.get('name', '')}({l.get('source', '')})" for l in layers[:10]
            ))
        if artifacts:
            lines.append("已有产物：" + "、".join(
                f"{a.get('type', '')}:{a.get('name', '')}" for a in artifacts[:10]
            ))
        last = ws.get("last_summary") or ""
        if last:
            lines.append(f"上一轮结论：{last}")
        if not lines:
            return ""
        summary = "\n".join(lines)
        if len(summary) <= max_chars:
            return summary
        return summary[: max_chars - 1] + "…"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _append_layer(ws: Dict[str, Any], layer: Dict[str, Any]):
        """按名称去重更新（同名图层只保留最新来源）。"""
        layers = ws.setdefault("layers", [])
        for l in layers:
            if l.get("name") == layer.get("name"):
                l.update(layer)
                return
        layers.append(layer)

    @staticmethod
    def _append_artifact(ws: Dict[str, Any], artifact: Dict[str, Any]):
        artifacts = ws.setdefault("artifacts", [])
        if not any(a.get("url") and a.get("url") == artifact.get("url") for a in artifacts):
            artifacts.append(artifact)

    def _save(self, session_id: str, ws: Dict[str, Any]):
        self._cache[session_id] = ws
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workspace_states (session_id, state_json, updated_at) VALUES (?,?,?)",
                (session_id, json.dumps(ws, ensure_ascii=False), _now_iso()),
            )
            conn.commit()


# 进程级单例（db_path 跟随 run_store）
_ws_singleton: Optional[WorkspaceState] = None


def get_workspace_state() -> WorkspaceState:
    global _ws_singleton
    if _ws_singleton is None:
        _ws_singleton = WorkspaceState(get_run_store().db_path)
    return _ws_singleton
