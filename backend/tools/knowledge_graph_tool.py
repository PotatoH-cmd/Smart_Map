#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知识图谱工具 — 原生 Kuzu 读写 + 8 类节点/8 类关系 + 文本转 Cypher 查询。

数据来源：
  - 知识库（90 文档 / 987 chunk）经 `kg_pipeline` 抽取，产出节点/关系并携带
    **完整溯源 refs**（doc_id + 章节路径 + 原文片段）。
  - PostgreSQL 测深表提供采区实测高程/深度/坐标，归一化后并入同名采区。

读写全部直连 kuzu 驱动（为何不用 LlamaIndex 的 KuzuPropertyGraphStore：其
`MERGE...SET` 硬编码固定列，自定义属性会被静默丢弃——见 kg_pipeline/writer.py）。
`kg_pipeline.writer` 是 CLI 与运行时共用的写入通道，保证两条路径列一致。

构建由 `scripts/kg_build.py build` 完成；本工具负责读 + 查询。
"""

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

KUZU_DB_DIR = os.path.join(os.path.dirname(__file__), "..", "kuzu_graph")
ARTIFACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "kg_pipeline", "out")


class KnowledgeGraphTool:
    """采砂知识图谱工具：读全图可视化 + Text-to-Cypher 查询。"""

    def __init__(self):
        self._llm = None
        self._db = None
        self._conn = None
        self._built = False

    # ─────────────────────────────────────────────
    # 对外接口
    # ─────────────────────────────────────────────

    def ensure_built(self) -> bool:
        """确保图谱已打开（延迟初始化）。"""
        if self._built:
            return True
        try:
            self._init_llm()
            self._init_graph()
            self._built = True
            return True
        except Exception as e:
            logger.error(f"[KG] 图谱初始化失败: {e}")
            return False

    def query(self, question: str) -> dict:
        """Text-to-Cypher 查询入口。返回 success/answer/cypher/raw_result/error。"""
        if not self.ensure_built():
            return {"success": False, "answer": "", "error": "知识图谱未就绪"}
        try:
            cypher = self._text_to_cypher(question)
            if not cypher:
                return {
                    "success": False, "answer": "无法将问题转换为图谱查询",
                    "cypher": "", "error": "Text-to-Cypher 生成失败",
                }
            raw = self._execute_cypher(cypher)
            answer = self._format_result(raw)
            return {
                "success": True, "answer": answer,
                "cypher": cypher, "raw_result": raw, "error": None,
            }
        except Exception as e:
            logger.error(f"[KG] 查询失败: {e}")
            return {"success": False, "answer": "", "error": str(e)}

    def get_graph_data(self) -> dict:
        """导出图谱为前端可视化格式（nodes/links/stats，含 type/refs）。"""
        if not self.ensure_built():
            return {"nodes": [], "links": [], "stats": {"county_count": 0, "area_count": 0}}
        try:
            from kg_pipeline.writer import read_graph
            return read_graph(self._conn)
        except Exception as e:
            logger.error(f"[KG] 导出图谱数据失败: {e}")
            return {"nodes": [], "links": [], "stats": {"county_count": 0, "area_count": 0}, "error": str(e)}

    # ─────────────────────────────────────────────
    # 初始化
    # ─────────────────────────────────────────────

    def _init_llm(self):
        from langchain_openai import ChatOpenAI

        self._llm = ChatOpenAI(
            model=os.environ.get("QA_LLM_MODEL", "qwen-plus"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            temperature=0.05,
        )
        logger.info("[KG] LLM 已初始化")

    def _init_graph(self):
        """打开 Kuzu 图谱；若为空则用产物自动重建（自愈）。"""
        import kuzu

        self._db = kuzu.Database(KUZU_DB_DIR)
        self._conn = kuzu.Connection(self._db)
        from kg_pipeline.writer import ensure_schema, is_empty
        ensure_schema(self._conn)

        if is_empty(self._conn):
            logger.info("[KG] 图谱为空，尝试用产物重建……")
            self._load_from_artifacts(self._conn)
            logger.info("[KG] 图谱重建完成")

    # ─────────────────────────────────────────────
    # 从产物加载（自愈路径；正常由 CLI 预构建）
    # ─────────────────────────────────────────────

    def _load_from_artifacts(self, conn) -> int:
        """读取 kg_pipeline/out/*.json 并把 PG 实测属性并入后写入图库。"""
        from kg_pipeline.postgres_source import load_pg_areas, merge_pg_into_nodes
        from kg_pipeline.writer import write_graph

        def load(name):
            path = os.path.join(ARTIFACTS_DIR, name)
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        nodes = load("nodes.json")
        relations = load("relations.json")
        if nodes is None or relations is None:
            raise RuntimeError(
                f"图谱为空且缺少产物 {ARTIFACTS_DIR}/*.json，"
                f"请先执行 scripts/kg_build.py extract && build --target {KUZU_DB_DIR}"
            )
        alias = {}
        vocab = load("area_vocab.json")
        if vocab:
            alias = vocab.get("alias", {})

        pg_rows = load_pg_areas(alias)
        merge_pg_into_nodes(nodes, pg_rows)
        rel_seen = {(r["rel"], r["src"], r["dst"]) for r in relations}
        # 知识库侧已有县区归属的采区，不再采用 PG 的县区列
        # （实测 PG 该列不可靠，如「李棚黄寨可采区」被标为潢川县而实属固始县）
        already = {r["src"] for r in relations if r.get("rel") == "BELONGS_TO"}
        for row in pg_rows:
            if not row["county"] or row["id"] in already:
                continue
            nodes.setdefault("County", {}).setdefault(row["county"], {
                "id": row["county"], "name": row["county"], "source": "postgres",
                "refs": "", "short": row["county"][:-1] if row["county"].endswith(("县", "区")) else row["county"],
            })
            key = ("BELONGS_TO", row["id"], row["county"])
            if key not in rel_seen:
                rel_seen.add(key)
                relations.append({"rel": "BELONGS_TO", "src": row["id"],
                                  "dst": row["county"], "source": "postgres"})
        stats = write_graph(conn, nodes, relations)
        return stats["nodes"]

    # ─────────────────────────────────────────────
    # Text-to-Cypher
    # ─────────────────────────────────────────────

    def _text_to_cypher(self, question: str) -> str:
        """用 LLM 将自然语言问题转为 Cypher 查询。"""
        from langchain_core.messages import HumanMessage
        from kg_pipeline.schema import schema_prompt
        from kg_pipeline.writer import NODE_COLUMNS

        sample_rows = self._entity_samples()

        prompt = f"""你是 Cypher 查询生成专家。根据以下图谱结构，将用户问题转为 Cypher 语句。

【图谱结构】
{schema_prompt()}

【节点表属性列】
{json.dumps({k: v for k, v in NODE_COLUMNS.items()}, ensure_ascii=False)}

【常见实体名参考（前 20 个采区/县区，用于精准匹配 name）】
{sample_rows}

Cypher 语法规则（Kuzu 方言）：
- 节点用圆括号，如 (a:County)；关系用方括号，如 -[r:BELONGS_TO]->
- 字符串用单引号，如 {{name: '罗山县'}}
- COUNT() 计数，WHERE 筛选，RETURN 返回；不支持 DISTINCT，用 COUNT(*)
- 关系 label 全局唯一：BELONGS_TO / LOCATED_IN_RIVER / LOCATED_IN_TOWN /
  ISSUED_FOR / DOCUMENTS_AREA / MENTIONS_RIVER / BELONGS_TO_PROJECT / PART_OF

约束：
- 只允许使用上表列出的属性列名，**列不存在就不要猜**
- 只能引用图谱结构中的类型与关系，不得虚构
- 只返回 Cypher 语句，不要任何解释或 markdown 标记

用户问题：{question}"""

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            cypher = resp.content.strip()
            cypher = re.sub(r"```(cypher|sql)?", "", cypher).replace("```", "").strip()
            logger.info(f"[KG] Text-to-Cypher: '{question[:50]}' → {cypher[:120]}")
            return cypher
        except Exception as e:
            logger.error(f"[KG] Text-to-Cypher 失败: {e}")
            return ""

    def _entity_samples(self) -> str:
        """取若干实体名供 LLM 精确匹配（提升 name 匹配准确率）。"""
        out = []
        for label, col in (("MineableArea", "name"), ("County", "name"),
                           ("River", "name"), ("License", "name")):
            try:
                r = self._conn.execute(f"MATCH (n:{label}) RETURN n.{col} LIMIT 20")
                names = []
                while r.has_next():
                    names.append(str(r.get_next()[0]))
                if names:
                    out.append(f"{label}: " + "、".join(names))
            except Exception:
                continue
        return "\n".join(out)

    def _execute_cypher(self, cypher: str) -> list:
        """执行 Cypher 并返回行。失败返回 [] 并记录错误（不再做脆弱降级）。"""
        try:
            result = self._conn.execute(cypher)
            col_names = result.get_column_names()
            rows = []
            while result.has_next():
                row = result.get_next()
                rows.append(self._serialize_cell(row))
            logger.info(f"[KG] Cypher 执行: {len(rows)} 行, 列: {col_names}")
            return rows
        except Exception as e:
            logger.error(f"[KG] Cypher 执行失败: {cypher[:100]} → {e}")
            return []

    @staticmethod
    def _serialize_cell(row) -> list:
        """把 Kuzu 行（可能含节点/关系对象）转成可 JSON 化的值。"""
        if not isinstance(row, (list, tuple)):
            row = [row]
        out = []
        for cell in row:
            if hasattr(cell, "items"):  # 节点/关系属性映射
                props = dict(cell)
                props.pop("_id", None)
                props.pop("_label", None)
                out.append(props)
            elif isinstance(cell, (int, float, str, bool)) or cell is None:
                out.append(cell)
            else:
                out.append(str(cell))
        return out

    def _format_result(self, raw_result: list) -> str:
        """将查询结果格式化为自然语言。"""
        if not raw_result:
            return "(图谱查询: 未找到匹配的数据，请参考文档检索结果)"
        if len(raw_result) == 1 and len(raw_result[0]) == 1:
            val = raw_result[0][0]
            if isinstance(val, (int, float)) and val == 0:
                return "(图谱查询: 返回 0 条记录，请参考文档检索结果)"
            return f"图谱精确结果: {self._render_val(val)}"
        lines = []
        for row in raw_result[:20]:
            lines.append(" | ".join(self._render_val(v) for v in row))
        return "\n".join(lines)

    @staticmethod
    def _render_val(v) -> str:
        if isinstance(v, dict):
            if v.get("name"):
                return str(v["name"])
            return json.dumps(v, ensure_ascii=False)
        return str(v)


# ─────────────────────────────────────────────
# 单例（避免重复初始化）
# ─────────────────────────────────────────────

_kg_instance: Optional[KnowledgeGraphTool] = None


def get_kg() -> KnowledgeGraphTool:
    global _kg_instance
    if _kg_instance is None:
        _kg_instance = KnowledgeGraphTool()
    return _kg_instance