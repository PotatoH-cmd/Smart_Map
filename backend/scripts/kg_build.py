#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知识图谱构建 CLI（两阶段：extract 抽取 → build 落库）。

    # 1. 抽取：知识库 → out/nodes.json + out/relations.json + out/dicts.json
    python scripts/kg_build.py extract

    # 2. 构建：产物 + PostgreSQL 实测属性 → Kuzu 图数据库
    python scripts/kg_build.py build --target kuzu_graph_new

    # 只校验产物（不落库）
    python scripts/kg_build.py validate

产物与图库分离：extract 可反复调参重跑（LLM 结果有 chunk 级缓存，不重复计费），
build 只读产物，可安全构建到新目录、校验通过后再切换。
"""

import argparse
import json
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("kg_build")

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
KB_DIR = os.path.join(BACKEND_DIR, "llama_index_storage")
OUT_DIR = os.path.join(BACKEND_DIR, "kg_pipeline", "out")


def _out(name: str) -> str:
    return os.path.join(OUT_DIR, name)


def _load_env():
    """加载 backend/.env（PG 密码、DASHSCOPE_API_KEY）。"""
    path = os.path.join(BACKEND_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ─────────────────────────────────────────────
# extract
# ─────────────────────────────────────────────

def cmd_extract(args):
    from kg_pipeline.area_vocab import mine_area_vocabulary, mine_license_area_relations
    from kg_pipeline.dictionaries import load_chunks, mine_dictionaries, mine_documents
    from kg_pipeline.llm_extractor import LLMExtractor, apply_llm_triples
    from kg_pipeline.rule_extractor import RuleExtractor
    from kg_pipeline.validators import format_report, validate_artifacts

    chunks = load_chunks(KB_DIR)
    logger.info(f"载入知识库 {len(chunks)} 个 chunk")

    # ── 1. 词表挖掘 ──
    dicts = mine_dictionaries(chunks)
    dicts["_documents"] = mine_documents(chunks)

    # ── 2. 采区名词表（第 1 遍：只从结构化锚点挖词，高精度）──
    vocab_info = mine_area_vocabulary(chunks)
    vocab, alias = vocab_info["vocab"], vocab_info["alias"]
    dicts["_license_area"] = mine_license_area_relations(chunks)
    logger.info(
        f"采区词表 {len(vocab)} 个（别名合并 {len(alias)} 个），"
        f"许可证绑定 {len(dicts['_license_area'])} 条"
    )

    # ── 3. 规则抽取（第 2 遍：词表回扫全文 + 溯源）──
    extractor = RuleExtractor(dicts, vocab, vocab_info.get("raw"))
    result = extractor.extract(chunks)
    nodes, relations = result["nodes"], result["relations"]
    hit_ids = result["hit_chunk_ids"]
    logger.info(f"规则命中 {len(hit_ids)}/{len(chunks)} chunk，关系 {len(relations)} 条")

    # ── 4. LLM 补漏（仅零命中 chunk）──
    zero_hit = [c for c in chunks if c.get("chunk_id") not in hit_ids]
    logger.info(f"零命中 chunk {len(zero_hit)} 个，进入 LLM 补漏")
    stats = {"added_nodes": 0, "added_relations": 0, "dropped": 0}
    if zero_hit and not args.no_llm:
        llm = LLMExtractor(dicts, cache_path=_out("llm_triples.json"))
        triples = llm.extract(zero_hit)
        rel_seen = {(r["rel"], r["src"], r["dst"]) for r in relations}
        stats = apply_llm_triples(nodes, relations, rel_seen, triples)
        logger.info(
            f"LLM 三元组 {len(triples)} 条 → 新增节点 {stats['added_nodes']}、"
            f"关系 {stats['added_relations']}，丢弃 {stats['dropped']}"
        )

    # ── 5. 校验 + 落盘 ──
    report = validate_artifacts(nodes, relations, alias)
    os.makedirs(OUT_DIR, exist_ok=True)
    _dump(_out("nodes.json"), nodes)
    _dump(_out("relations.json"), relations)
    _dump(_out("dicts.json"), dicts)
    _dump(_out("area_vocab.json"), vocab_info)

    logger.info("── 节点统计 ──")
    for ntype, bucket in nodes.items():
        logger.info(f"  {ntype:<14} {len(bucket)}")
    print(format_report(report))
    if stats["added_nodes"]:
        print(f"LLM 补充：节点 +{stats['added_nodes']}，关系 +{stats['added_relations']}")
    print(f"产物已写入 {OUT_DIR}")


# ─────────────────────────────────────────────
# build
# ─────────────────────────────────────────────

def _read_artifacts():
    for name in ("nodes.json", "relations.json"):
        if not os.path.exists(_out(name)):
            raise SystemExit(f"缺少产物 {_out(name)}，请先执行 extract")
    nodes = _load(_out("nodes.json"))
    relations = _load(_out("relations.json"))
    vocab_info = _load(_out("area_vocab.json")) if os.path.exists(_out("area_vocab.json")) else {}
    return nodes, relations, vocab_info


def cmd_build(args):
    from kg_pipeline.postgres_source import load_pg_areas, merge_pg_into_nodes
    from kg_pipeline.validators import format_report, validate_artifacts
    from kg_pipeline.writer import table_counts, write_graph

    nodes, relations, vocab_info = _read_artifacts()
    alias = vocab_info.get("alias", {})

    # ── 1. 并入 PostgreSQL 实测属性 ──
    if not args.no_pg:
        pg_rows = load_pg_areas(alias)
        pg_stats = merge_pg_into_nodes(nodes, pg_rows)
        # PG 采区的县区归属关系（PG 的县区列只覆盖部分县区）
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
        logger.info(f"PostgreSQL 合并：并入 {pg_stats['merged']} 个已有采区，新增 {pg_stats['new']} 个")

    # ── 2. 建库并写入 ──
    import kuzu
    target = args.target if os.path.isabs(args.target) else os.path.join(BACKEND_DIR, args.target)
    if os.path.exists(target):
        if not args.force:
            raise SystemExit(f"目标已存在：{target}（加 --force 覆盖）")
        shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
    db = kuzu.Database(target)
    conn = kuzu.Connection(db)
    stats = write_graph(conn, nodes, relations)

    # ── 3. 校验 ──
    report = validate_artifacts(nodes, relations, alias)
    print(format_report(report))
    print(f"\n图谱已构建：{target}")
    for ntype, cnt in table_counts(conn).items():
        print(f"  {ntype:<14} {cnt}")
    print(f"  {'(关系)':<14} {stats['relations']}")


def cmd_validate(_args):
    from kg_pipeline.validators import format_report, validate_artifacts

    nodes, relations, vocab_info = _read_artifacts()
    report = validate_artifacts(nodes, relations, vocab_info.get("alias", {}))
    print(format_report(report))
    raise SystemExit(0 if report["ok"] else 1)


# ─────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────

def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="知识图谱构建 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="从知识库抽取实体关系（不落库）")
    p_ext.add_argument("--no-llm", action="store_true", help="跳过 LLM 补漏（只跑规则）")
    p_ext.set_defaults(func=cmd_extract)

    p_build = sub.add_parser("build", help="构建 Kuzu 图数据库")
    p_build.add_argument("--target", default="kuzu_graph_new", help="目标目录/文件")
    p_build.add_argument("--force", action="store_true", help="目标存在时覆盖")
    p_build.add_argument("--no-pg", action="store_true", help="不并入 PostgreSQL 实测属性")
    p_build.set_defaults(func=cmd_build)

    p_val = sub.add_parser("validate", help="校验抽取产物")
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    _load_env()
    args.func(args)


if __name__ == "__main__":
    main()
