#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识图谱工具 — 基于 LlamaIndex PropertyGraphIndex + Kuzu 嵌入式图数据库

从 PostgreSQL ceshen 表导入实体和关系，构建采砂场知识图谱，
支持 Text-to-Cypher 自然语言查询，补充向量检索无法精确回答的实体统计问题。
"""

import json
import logging
import os
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# ── PostgreSQL 配置 ──
PG_CONFIG = {
    "host": os.environ.get("GEOSERVER_PG_HOST", "172.136.16.52"),
    "port": int(os.environ.get("GEOSERVER_PG_PORT", "5432")),
    "database": os.environ.get("GEOSERVER_PG_DB", "postgres"),
    "user": os.environ.get("GEOSERVER_PG_USER", "postgres"),
    "password": os.environ.get("GEOSERVER_PG_PASSWORD", "8720622"),
}

# ── Kuzu 图谱存储路径 ──
KUZU_DB_DIR = os.path.join(os.path.dirname(__file__), "..", "kuzu_graph")
KUZU_META_FILE = os.path.join(os.path.dirname(__file__), "..", "kuzu_graph_meta.json")


class KnowledgeGraphTool:
    """
    采砂场知识图谱工具

    - 从 PostgreSQL ceshen 表导入县区和采砂场实体
    - 建立 BELONGS_TO 关系
    - 支持 Text-to-Cypher 自然语言查询
    """

    def __init__(self):
        self._index: Any = None       # PropertyGraphIndex 实例
        self._store: Any = None       # KuzuPropertyGraphStore 实例
        self._llm = None              # ChatOpenAI 用于 Text-to-Cypher
        self._built = False
        self._node_meta = {}         # 节点属性元数据（Kuzu不存自定义属性，补存在此）

    # ─────────────────────────────────────────────
    # 对外接口
    # ─────────────────────────────────────────────

    def ensure_built(self) -> bool:
        """确保图谱已构建（延迟初始化，首次调用时触发）"""
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
        """
        Text-to-Cypher 查询入口

        返回:
        {
            "success": bool,
            "answer": str,          # 自然语言回答
            "cypher": str,          # 生成的 Cypher 语句
            "raw_result": list,     # 数据库原始返回
            "error": str or None,
        }
        """
        if not self.ensure_built():
            return {"success": False, "answer": "", "error": "知识图谱未就绪"}

        try:
            cypher = self._text_to_cypher(question)
            if not cypher:
                return {
                    "success": False,
                    "answer": "无法将问题转换为图谱查询",
                    "cypher": "",
                    "error": "Text-to-Cypher 生成失败",
                }

            raw = self._execute_cypher(cypher)
            answer = self._format_result(question, raw, cypher)

            return {
                "success": True,
                "answer": answer,
                "cypher": cypher,
                "raw_result": raw,
                "error": None,
            }
        except Exception as e:
            logger.error(f"[KG] 查询失败: {e}")
            return {"success": False, "answer": "", "error": str(e)}

    def get_graph_data(self) -> dict:
        """导出图谱数据为可视化格式

        返回:
        {
            "nodes": [{"id": "...", "name": "...", "category": 0|1, ...}],
            "links": [{"source": "...", "target": "...", "label": "..."}],
            "stats": {"county_count": N, "area_count": M}
        }
        """
        if not self.ensure_built():
            return {"nodes": [], "links": [], "stats": {"county_count": 0, "area_count": 0}}

        # 加载属性元数据
        self._load_metadata()

        import kuzu
        db = kuzu.Database(KUZU_DB_DIR)
        conn = kuzu.Connection(db)

        nodes = []
        links = []
        county_count = 0
        area_count = 0

        try:
            # 查询所有 County 节点
            result = conn.execute("MATCH (c:County) RETURN c")
            while result.has_next():
                row = result.get_next()
                c = row[0]
                props = dict(c) if hasattr(c, '__iter__') else {}
                node_name = str(props.get('name', ''))
                # 合并元数据属性
                meta = self._node_meta.get(node_name, {})
                merged_props = {**meta, **{k: v for k, v in props.items() if k not in ('_id', '_label', 'creation_date', 'last_modified_date')}}
                nodes.append({
                    "id": node_name,
                    "name": node_name,
                    "category": 0,  # 0 = 县区
                    "symbolSize": 40,
                    "itemStyle": {"color": "#0ea5e9"},
                    "properties": merged_props
                })
                county_count += 1

            # 查询所有 MineableArea 节点
            result2 = conn.execute("MATCH (a:MineableArea) RETURN a")
            while result2.has_next():
                row = result2.get_next()
                a = row[0]
                props = dict(a) if hasattr(a, '__iter__') else {}
                node_name = str(props.get('name', ''))
                area_id = f"{node_name}_{props.get('year', '')}"
                # 合并元数据属性（通过完整 node name 匹配）
                meta = self._node_meta.get(area_id, self._node_meta.get(node_name, {}))
                merged_props = {**meta, **{k: v for k, v in props.items() if k not in ('_id', '_label', 'creation_date', 'last_modified_date')}}
                county = merged_props.get('county', '')
                nodes.append({
                    "id": area_id,
                    "name": node_name,
                    "category": 1,  # 1 = 采砂场
                    "symbolSize": 20,
                    "itemStyle": {"color": "#f59e0b"},
                    "properties": merged_props
                })
                area_count += 1

            # 查询所有 BELONGS_TO 关系（不依赖 year 属性，直接查全节点）
            result3 = conn.execute(
                "MATCH (a:MineableArea)-[r:BELONGS_TO]->(c:County) "
                "RETURN a, c"
            )
            while result3.has_next():
                row = result3.get_next()
                a = row[0]
                c = row[1]
                a_props = dict(a) if hasattr(a, '__iter__') else {}
                c_props = dict(c) if hasattr(c, '__iter__') else {}
                area_name = str(a_props.get('name', ''))
                area_year = a_props.get('year', 'unknown')
                county_name = str(c_props.get('name', ''))
                source_id = f"{area_name}_{area_year}"
                links.append({
                    "source": source_id,
                    "target": county_name,
                    "label": "BELONGS_TO",
                })

            # 去重节点（按 id）
            seen = set()
            unique_nodes = []
            for node in nodes:
                if node["id"] not in seen:
                    seen.add(node["id"])
                    unique_nodes.append(node)

            return {
                "nodes": unique_nodes,
                "links": links,
                "stats": {
                    "county_count": county_count,
                    "area_count": area_count,
                }
            }
        except Exception as e:
            logger.error(f"[KG] 导出图谱数据失败: {e}")
            return {"nodes": [], "links": [], "stats": {"county_count": 0, "area_count": 0}, "error": str(e)}

    # ─────────────────────────────────────────────
    # 初始化
    # ─────────────────────────────────────────────

    def _init_llm(self):
        """初始化 LLM（用于 Text-to-Cypher）"""
        from langchain_openai import ChatOpenAI

        model_name = os.environ.get("QA_LLM_MODEL", "qwen-plus")
        self._llm = ChatOpenAI(
            model=model_name,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048"),
            temperature=0.05,
        )
        logger.info("[KG] LLM 已初始化")

    def _init_graph(self):
        """初始化 Kuzu 图存储和 PropertyGraphIndex，如已持久化则加载，否则从 PostgreSQL 构建"""
        from llama_index.graph_stores.kuzu import KuzuPropertyGraphStore
        from llama_index.core.indices.property_graph import PropertyGraphIndex
        from llama_index.core import Settings
        from llama_index.embeddings.dashscope import DashScopeEmbedding
        import kuzu

        # 配置全局 embedding 和 LLM（PropertyGraphIndex 需要，但实际由 langchain 驱动查询）
        Settings.embed_model = DashScopeEmbedding(
            model_name=os.environ.get("LLAMAINDEX_EMBED_MODEL", "text-embedding-v3"),
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            embed_batch_size=10,
        )
        from llama_index.llms.openai import OpenAI
        Settings.llm = OpenAI(
            model=os.environ.get("QA_LLM_MODEL", "qwen-plus"),
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            temperature=0.05,
        )

        # Kuzu 0.11.x 数据库是一个文件，不是目录
        # 如果路径是目录则先删除（可能是之前版本残留）
        if os.path.isdir(KUZU_DB_DIR):
            import shutil
            shutil.rmtree(KUZU_DB_DIR)
            logger.info("[KG] 删除了旧的目录残留，Kuzu 将创建数据库文件")

        db = kuzu.Database(KUZU_DB_DIR)
        self._store = KuzuPropertyGraphStore(
            db=db,
            has_structured_schema=True,
            relationship_schema=[
                ("MineableArea", "BELONGS_TO", "County"),
            ],
            sanitize_query_output=True,
            use_vector_index=False,  # 纯图谱检索不需要向量索引
        )

        # 检查是否已有数据（尝试列出节点表）
        conn = kuzu.Connection(db)
        try:
            tables_result = conn.execute(
                "SELECT name FROM information_schema.tables "
                "WHERE name IN ('County', 'MineableArea', 'BELONGS_TO')"
            )
            existing = set()
            while tables_result.has_next():
                existing.add(tables_result.get_next()[0])

            if len(existing) >= 2:  # 至少有节点表
                logger.info(f"[KG] 从已有 Kuzu 数据库加载，现有表: {existing}")
                # 加载已保存的属性元数据
                if not self._node_meta:
                    self._load_metadata()
            else:
                logger.info("[KG] Kuzu 数据库为空，从 PostgreSQL 构建图谱...")
                self._build_from_postgres()
                logger.info("[KG] PostgreSQL 数据已导入，开始从文档抽取...")
                self._build_from_documents()
                logger.info("[KG] 图谱构建完成")
        except Exception as e:
            logger.info(f"[KG] 检测表结构时出错，将重新构建: {e}")
            self._build_from_postgres()
            try:
                self._build_from_documents()
            except Exception as e2:
                logger.warning(f"[KG] 文档抽取跳过: {e2}")

        # 保存节点属性元数据到文件
        self._save_metadata()

        # 创建 PropertyGraphIndex
        self._index = PropertyGraphIndex.from_existing(
            property_graph_store=self._store,
        )
        logger.info("[KG] PropertyGraphIndex 已就绪")

    # ─────────────────────────────────────────────
    # 图谱构建（从 PostgreSQL）
    # ─────────────────────────────────────────────

    # ─────────────────────────────────────────────
    # 图谱构建（从 LlamaIndex 文档抽取实体）
    # ─────────────────────────────────────────────

    def _build_from_documents(self):
        """从 LlamaIndex 文档中抽取县区和采砂场实体，写入图谱

        对每个县区搜索相关文档 chunk → LLM 提取结构化 JSON → upsert 节点和关系
        """
        from llama_index.core import Settings, StorageContext, load_index_from_storage
        from llama_index.embeddings.dashscope import DashScopeEmbedding
        from llama_index.core.graph_stores.types import EntityNode, Relation

        # ── 1. 加载向量索引 ──
        persist_dir = os.path.join(os.path.dirname(__file__), "..", "llama_index_storage")
        if not os.path.exists(os.path.join(persist_dir, "docstore.json")):
            logger.warning("[KG] 向量索引文件不存在，跳过文档抽取")
            return

        Settings.embed_model = DashScopeEmbedding(
            model_name=os.environ.get("LLAMAINDEX_EMBED_MODEL", "text-embedding-v3"),
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            embed_batch_size=10,
        )
        storage_context = StorageContext.from_defaults(persist_dir=persist_dir)
        vector_index = load_index_from_storage(storage_context)
        logger.info("[KG] 已加载向量索引，开始从文档抽取实体")

        # ── 2. 定义要搜索的县区 ──
        counties = [
            "罗山县", "平桥区", "商城县", "光山县",
            "潢川县", "息县", "淮滨县", "新县"
        ]
        # 固始县已有 PostgreSQL 数据，但文档可能有补充信息

        retriever = vector_index.as_retriever(similarity_top_k=15)

        all_county_nodes: Dict[str, EntityNode] = {}
        all_mineable_nodes: List[EntityNode] = []
        all_relations: List[Relation] = []

        for county in counties:
            try:
                # ── 3. 搜索相关文档 ──
                query = f"{county} 批复采区 采砂场 可采区 开采情况"
                retrieved = retriever.retrieve(query)

                # 过滤含采砂关键词的 chunk
                keywords = ["批复采区", "采砂场", "可采区", "采砂", "开采", "砂场"]
                relevant_chunks = [
                    node.text for node in retrieved
                    if any(kw in node.text for kw in keywords)
                ]

                logger.info(
                    f"[KG] {county}: 检索到 {len(retrieved)} chunk, "
                    f"含采砂关键词 {len(relevant_chunks)} chunk"
                )

                if not relevant_chunks:
                    logger.info(f"[KG] {county} 无采砂相关文档，跳过")
                    continue

                # ── 4. LLM 提取结构化数据 ──
                context = "\n\n---\n\n".join(relevant_chunks[:10])
                # 截断过长上下文
                if len(context) > 6000:
                    context = context[:6000] + "\n...(内容过长已截断)"

                extracted = self._extract_entities_from_text(county, context)
                if not extracted or not extracted.get("areas"):
                    logger.info(f"[KG] {county}: LLM 未提取到采砂场实体")
                    continue

                county_name = extracted.get("county", county)
                logger.info(
                    f"[KG] {county}: LLM 提取到 {len(extracted.get('areas', []))} 个采砂场"
                )

                # ── 5. 写入图谱 ──
                if county_name not in all_county_nodes:
                    all_county_nodes[county_name] = EntityNode(
                        name=county_name,
                        label="County",
                        properties={
                            "name": county_name,
                            "source": "document",
                            "total_approved": extracted.get("total_approved", 0),
                            "total_active": extracted.get("total_active", 0),
                        },
                    )

                for area in extracted.get("areas", []):
                    area_name = area.get("name", "")
                    if not area_name:
                        continue
                    node_name = f"{area_name}_{area.get('year', 'unknown')}"
                    node = EntityNode(
                        name=node_name,
                        label="MineableArea",
                        properties={
                            "name": area_name,
                            "county": county_name,
                            "year": area.get("year", 0),
                            "license_id": area.get("license_id", ""),
                            "river": area.get("river", ""),
                            "is_active": area.get("is_active", True),
                            "source": "document",
                        },
                    )
                    all_mineable_nodes.append(node)
                    all_relations.append(Relation(
                        label="BELONGS_TO",
                        source_id=node_name,
                        target_id=county_name,
                        properties={"source": "document"},
                    ))

            except Exception as e:
                logger.warning(f"[KG] {county} 实体抽取异常: {e}")
                continue

        # ── 6. 批量写入图谱 ──
        if all_mineable_nodes:
            # 保存节点属性元数据
            for node_name, node in all_county_nodes.items():
                self._node_meta[node_name] = dict(node.properties)
            for node in all_mineable_nodes:
                self._node_meta[node.name] = dict(node.properties)

            all_nodes = list(all_county_nodes.values()) + all_mineable_nodes
            self._store.upsert_nodes(all_nodes)
            self._store.upsert_relations(all_relations)
            logger.info(
                f"[KG] 文档抽取完成: {len(all_county_nodes)} 县区, "
                f"{len(all_mineable_nodes)} 采砂场, {len(all_relations)} 关系"
            )
        else:
            logger.info("[KG] 文档抽取未发现新的采砂场实体")

    def _extract_entities_from_text(self, county: str, context: str) -> dict:
        """用 LLM 从文档片段中提取采砂场结构化数据

        Args:
            county: 县区名（如 "罗山县"）
            context: 相关文档文本

        Returns:
            {"county": "罗山县", "total_approved": 12, "total_active": 11,
             "areas": [{"name": "...", "year": 2023, "river": "...", "license_id": "..."}]}
        """
        from langchain_core.messages import HumanMessage

        prompt = f"""你是数据提取专家。从以下关于 {county} 采砂管理的文档中，提取采砂场信息。

请输出纯 JSON 格式（不要 markdown 标记），结构如下：
{{
  "county": "{county}",
  "total_approved": 批复采区总数（整数）,
  "total_active": 实际开采数量（整数）,
  "areas": [
    {{"name": "采砂场名称", "year": 年份, "river": "所在河流", "license_id": "许可证号", "is_active": true/false}}
  ]
}}

如果某些字段无法从文档中确定，填 null 或 0。
如果文档中完全没有采砂场信息，返回 {{"county": "{county}", "areas": []}}。

文档内容：
{context}"""

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            text = resp.content.strip()
            # 清理 markdown 标记
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            return result
        except json.JSONDecodeError as e:
            logger.warning(f"[KG] LLM 提取 JSON 解析失败: {e}, 原始输出: {text[:200]}")
            # 降级：尝试截取第一个 { 到最后一个 }
            try:
                start = text.index('{')
                end = text.rindex('}') + 1
                result = json.loads(text[start:end])
                return result
            except (ValueError, json.JSONDecodeError):
                return {"county": county, "areas": []}
        except Exception as e:
            logger.error(f"[KG] LLM 提取失败: {e}")
            return {"county": county, "areas": []}

    def _save_metadata(self):
        """保存节点属性元数据到 JSON 文件"""
        try:
            with open(KUZU_META_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._node_meta, f, ensure_ascii=False, indent=2)
            logger.info(f"[KG] 元数据已保存: {len(self._node_meta)} 个节点")
        except Exception as e:
            logger.warning(f"[KG] 保存元数据失败: {e}")

    def _load_metadata(self):
        """从 JSON 文件加载节点属性元数据"""
        if os.path.exists(KUZU_META_FILE):
            try:
                with open(KUZU_META_FILE, 'r', encoding='utf-8') as f:
                    self._node_meta = json.load(f)
                logger.info(f"[KG] 元数据已加载: {len(self._node_meta)} 个节点")
                return True
            except Exception as e:
                logger.warning(f"[KG] 加载元数据失败: {e}")
        return False

    def _build_from_postgres(self):
        """从 PostgreSQL ceshen 表提取数据，构建图谱节点和关系"""
        try:
            import psycopg2
        except ImportError:
            raise ImportError("psycopg2 未安装，无法从 PostgreSQL 构建图谱")

        conn = psycopg2.connect(**PG_CONFIG)
        cur = conn.cursor()

        # 查所有可采区（按名称和年份聚合，计算平均高程和深度）
        cur.execute("""
            SELECT
                "Mineable_Area_Name",
                "County_District",
                "Year",
                "Mineable_Area_ID",
                AVG("Control_Elevation")::numeric(10,2) AS avg_control_elevation,
                AVG("Measured_Depth")::numeric(10,2) AS avg_measured_depth,
                AVG("Lon_4326")::numeric(10,6) AS lon,
                AVG("Lat_4326")::numeric(10,6) AS lat,
                COUNT(*) AS measurement_count
            FROM ceshen
            GROUP BY "Mineable_Area_Name", "County_District", "Year", "Mineable_Area_ID"
            ORDER BY "County_District", "Mineable_Area_Name"
        """)

        from llama_index.core.graph_stores.types import EntityNode, Relation

        county_nodes: Dict[str, EntityNode] = {}
        mineable_nodes: List[EntityNode] = []
        relations: List[Relation] = []

        for row in cur.fetchall():
            name = row[0]
            county = row[1]
            year = row[2]
            license_id = row[3]
            avg_ctrl = float(row[4]) if row[4] else 0
            avg_depth = float(row[5]) if row[5] else 0
            lon = float(row[6]) if row[6] else 0
            lat = float(row[7]) if row[7] else 0
            m_count = row[8]

            # 县区节点（去重）
            if county not in county_nodes:
                county_nodes[county] = EntityNode(
                    name=county,
                    label="County",
                    properties={"name": county},
                )

            # 采砂场节点（name + year 保证唯一）
            node_name = f"{name}_{year}"
            node = EntityNode(
                name=node_name,
                label="MineableArea",
                properties={
                    "name": name,
                    "county": county,
                    "year": int(year) if year else 0,
                    "license_id": license_id or "",
                    "avg_control_elevation": avg_ctrl,
                    "avg_measured_depth": avg_depth,
                    "lon": lon,
                    "lat": lat,
                    "measurement_count": m_count,
                },
            )
            mineable_nodes.append(node)

            # 关系: MineableArea → County
            relations.append(Relation(
                label="BELONGS_TO",
                source_id=node_name,
                target_id=county,
                properties={"year": int(year) if year else 0},
            ))

        cur.close()
        conn.close()

        # 保存节点属性元数据
        for node_name, node in county_nodes.items():
            self._node_meta[node_name] = dict(node.properties)
        for node in mineable_nodes:
            self._node_meta[node.name] = dict(node.properties)

        # 写入图谱
        all_nodes = list(county_nodes.values()) + mineable_nodes
        logger.info(
            f"[KG] 构建节点: {len(county_nodes)} 个县区, "
            f"{len(mineable_nodes)} 个采砂场, {len(relations)} 条关系"
        )
        self._store.upsert_nodes(all_nodes)
        self._store.upsert_relations(relations)
        logger.info("[KG] 节点和关系已写入 Kuzu")

    # ─────────────────────────────────────────────
    # Text-to-Cypher
    # ─────────────────────────────────────────────

    def _text_to_cypher(self, question: str) -> str:
        """用 LLM 将自然语言问题转为 Cypher 查询"""
        from langchain_core.messages import HumanMessage

        prompt = f"""你是一个 Cypher 查询生成专家。根据以下图谱结构，将用户问题转为 Cypher 语句。

图谱结构：
- 节点类型：
  - County（县区），属性：name
  - MineableArea（采砂场），属性：name, county, year, license_id, avg_control_elevation, avg_measured_depth, lon, lat, measurement_count
- 关系：
  - MineableArea -[BELONGS_TO]-> County

Cypher 语法规则（Kuzu 方言）：
- 节点用圆括号表示，如 (a:County)
- 关系用方括号表示，如 -[r:BELONGS_TO]->
- 字符串用单引号，如 {{name: '罗山县'}}
- COUNT() 计数，WHERE 筛选，RETURN 返回
- 不支持 DISTINCT，改用 COUNT(*)

用户问题：{question}

请只返回 Cypher 查询语句，不要任何解释或 markdown 标记。"""

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            cypher = resp.content.strip()
            # 清理 markdown 标记
            cypher = cypher.replace("```cypher", "").replace("```sql", "").replace("```", "").strip()
            logger.info(f"[KG] Text-to-Cypher: '{question[:50]}' → {cypher[:120]}")
            return cypher
        except Exception as e:
            logger.error(f"[KG] Text-to-Cypher 失败: {e}")
            return ""

    def _execute_cypher(self, cypher: str) -> list:
        """执行 Cypher 查询并返回结果"""
        import kuzu

        db = kuzu.Database(KUZU_DB_DIR)
        conn = kuzu.Connection(db)
        try:
            result = conn.execute(cypher)
            rows = []
            col_names = []
            while result.has_next():
                row = result.get_next()
                if not col_names:
                    # 首行获取列名
                    if hasattr(result, 'get_column_names'):
                        col_names = result.get_column_names()
                    elif hasattr(result, 'columns'):
                        col_names = result.columns
                rows.append(list(row))
            logger.info(f"[KG] Cypher 执行: {len(rows)} 行, 列: {col_names}")
            return rows
        except Exception as e:
            logger.error(f"[KG] Cypher 执行失败: {cypher[:100]} → {e}")

            # 降级：尝试更简单的查询
            try:
                # 重新连接（kuzu 可能不支持事务重试）
                db2 = kuzu.Database(KUZU_DB_DIR)
                conn2 = kuzu.Connection(db2)

                # 尝试最简单的计数查询
                if "count" in cypher.lower() and "County" in cypher:
                    # 提取县名
                    import re
                    match = re.search(r"'([^']+)'", cypher)
                    if match:
                        county_name = match.group(1)
                        result2 = conn2.execute(
                            f"MATCH (a:MineableArea)-[r:BELONGS_TO]->(c:County) "
                            f"WHERE c.name = '{county_name}' RETURN a.name"
                        )
                        rows2 = []
                        while result2.has_next():
                            rows2.append(list(result2.get_next()))
                        logger.info(f"[KG] 降级查询成功: {len(rows2)} 行")
                        return rows2
                return []
            except Exception as e2:
                logger.error(f"[KG] 降级查询也失败: {e2}")
                return []

    def _format_result(self, question: str, raw_result: list, cypher: str) -> str:
        """将原始查询结果格式化为自然语言"""
        if not raw_result:
            return "(图谱查询: 未找到匹配的数据，请参考文档检索结果)"

        # 简单格式化
        if len(raw_result) == 1 and len(raw_result[0]) == 1:
            val = raw_result[0][0]
            if isinstance(val, (int, float)) and val == 0:
                return "(图谱查询: 返回 0 条记录，该地区可能不在数据库范围内，请参考文档检索结果)"
            return f"图谱精确结果: {val}"

        # 多行结果：列出
        lines = []
        for row in raw_result[:20]:
            lines.append(" | ".join(str(v) for v in row))
        return "\n".join(lines)


# ─────────────────────────────────────────────
# 单例（避免重复初始化）
# ─────────────────────────────────────────────

_kg_instance: Optional[KnowledgeGraphTool] = None


def get_kg() -> KnowledgeGraphTool:
    global _kg_instance
    if _kg_instance is None:
        _kg_instance = KnowledgeGraphTool()
    return _kg_instance
