"""知识库：文档检索/问答/上传/图谱（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from tools.knowledge_graph_tool import get_kg
import asyncio
from tools.knowledge_qa_agent import KnowledgeQAAgent
import os
# 知识库后端选择（core.config 单一来源：KNOWLEDGE_BACKEND: ragflow|llamaindex）
from core.config import runtime as _cfg_runtime
_KB_BACKEND = _cfg_runtime.knowledge_backend
if _KB_BACKEND == "llamaindex":
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from tools.ragflow_knowledge_tool import KnowledgeBaseTool
import logging


logger = logging.getLogger(__name__)

router = APIRouter()


class KnowledgeItem(BaseModel):
    title: str
    content: str
    tags: List[str] = []
class KnowledgeQARequest(BaseModel):
    question: str
    top_k: int = 5
class KnowledgeAddRequest(BaseModel):
    name: str
    content: str
@router.get("/api/knowledge")
async def list_knowledge(project: str = None):
    try:
        kb_tool = KnowledgeBaseTool()
        req_params = {'operation': 'list_topics'}
        if project:
            req_params['project'] = project
        result = kb_tool.call(req_params)
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"List knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.get("/api/knowledge/folders")
async def list_knowledge_folders():
    """列出知识库项目文件夹及各自文档数（供前端“选择文件夹”入口）。

    计数口径为**文档级**（按 doc_id 去重），与文档列表一致；
    旧的 list_folders 走切片级，会把「N 个文档」显示成切片数。
    """
    try:
        from kb_pipeline import Manifest
        from collections import defaultdict

        m = Manifest()
        agg = defaultdict(lambda: {"count": 0, "chunks": 0})
        for doc in m.all_documents():
            key = doc.get("project") or ""
            slot = agg[key]
            slot["count"] += 1
            slot["chunks"] += len(m.chunk_fps(doc.get("doc_id")))
            if doc.get("project_name"):
                slot["project_name"] = doc["project_name"]
        folders = [
            {
                "project": key,
                "project_name": val.get("project_name") or "未分组",
                "count": val["count"],
                "chunk_count": val["chunks"],
            }
            for key, val in agg.items()
        ]
        folders.sort(key=lambda x: (-x["count"], x["project_name"]))
        return {
            "success": True,
            "folders": folders,
            "total_documents": sum(f["count"] for f in folders),
            "total_folders": len(folders),
        }
    except Exception as e:
        logger.error(f"List knowledge folders error: {e}")
        # 兼容：清单不可用时回退到工具实现，至少不阻塞入口
        try:
            kb_tool = KnowledgeBaseTool()
            result = kb_tool.call({'operation': 'list_folders'})
            if result.get('success'):
                return result
        except Exception:
            pass
        return {'success': True, 'folders': [], 'total_folders': 0, 'total_documents': 0}
@router.get("/api/knowledge/graph")
async def get_knowledge_graph():
    """知识图谱可视化数据接口 — 返回节点和关系用于前端力导向图渲染"""
    try:
        kg = get_kg()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, kg.get_graph_data)
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Get knowledge graph error: {e}")
        return {"success": False, "error": str(e)}
@router.get("/api/knowledge/stale")
async def list_stale_documents():
    """列出源文件已改、但库内仍是旧版的文档（切片过期检测）。

    注意：本路由必须定义在 /api/knowledge/{document_id} 之前，
    否则 "stale" 会被当作 document_id 匹配。
    """
    try:
        from kb_pipeline import Manifest, scan_stale
        stale = scan_stale(Manifest())
        return {"success": True, "count": len(stale), "stale": stale}
    except Exception as e:
        logger.error(f"List stale documents error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/{document_id}/chunks")
async def list_document_chunks(document_id: str):
    """返回某文档的切片清单（指纹 + node_id），供排查增量状态。"""
    try:
        from kb_pipeline import Manifest
        m = Manifest()
        doc = m.lookup("doc_id", document_id)
        node_map = m.node_ids_of(document_id)
        chunks = [
            {"chunk_fp": fp, "node_ids": nids, "duplicated": len(nids) > 1}
            for fp, nids in node_map.items()
        ]
        return {
            "success": True,
            "document_id": document_id,
            "name": (doc or {}).get("name"),
            "chunk_count": len(chunks),
            "source_file": (doc or {}).get("source_file"),
            "source_mtime": (doc or {}).get("source_mtime"),
            "chunks": chunks,
        }
    except Exception as e:
        logger.error(f"List document chunks error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/documents")
async def list_kb_documents(
    project: str = Query(None, description="按项目过滤（传 project 标识）"),
    q: str = Query(None, description="按名称关键词过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
):
    """文档处理页的文档目录。

    注意：GET /api/knowledge 返回的是切片级记录（每篇文档含多条），
    本接口按 doc_id 聚合到文档级，并标注切片数、项目归属、是否登记源文件、
    是否过期。路由必须声明在 /api/knowledge/{document_id} 之前，否则
    "documents" 会被当成 document_id 匹配。
    """
    try:
        from kb_pipeline import Manifest

        m = Manifest()
        stale_ids = set()
        try:
            from kb_pipeline import scan_stale
            stale_ids = {s["doc_id"] for s in scan_stale(m)}
        except Exception:
            pass  # 过期判定失败不阻塞列表

        def name_key(name):
            # 去掉内部前缀（如 section_5_0007_）与 .txt 后缀再排序
            import re
            key = re.sub(r"^section_\d+_\d+_", "", name or "")
            if key.lower().endswith(".txt"):
                key = key[:-4]
            return key

        items = []
        for doc in m.all_documents():
            doc_id = doc.get("doc_id")
            if not doc_id:
                continue
            if project and doc.get("project") != project:
                continue
            name = doc.get("name") or doc_id
            if q and (q.lower() not in name.lower() and q.lower() not in doc_id.lower()):
                continue
            src = doc.get("source_file")
            src_size = None
            if src and os.path.isfile(src):
                try:
                    src_size = os.path.getsize(src)
                except OSError:
                    src_size = None
            items.append({
                "doc_id": doc_id,
                "name": name,
                "project": doc.get("project"),
                "project_name": doc.get("project_name"),
                "chunk_count": len(m.chunk_fps(doc_id)),
                "source_file": os.path.basename(src) if src else None,
                "has_source_file": bool(src),
                "source_size": src_size,
                "stale": doc_id in stale_ids,
                "updated_at": doc.get("updated_at"),
            })

        items.sort(key=lambda x: (-x["chunk_count"], name_key(x["name"])))
        total = len(items)
        total_chunks = sum(x["chunk_count"] for x in items)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        return {
            "success": True,
            "count": total,
            "total_chunks": total_chunks,
            "with_source_file": sum(1 for x in items if x["has_source_file"]),
            "page": page,
            "page_size": page_size,
            "documents": page_items,
            "range": {"from": start, "to": min(start + page_size, total) - 1, "total": total},
        }
    except Exception as e:
        logger.error(f"List kb documents error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/{document_id}/raw")
async def get_raw_document(document_id: str):
    """返回文档的「未处理」原文，供文档处理与预览页左栏展示。

    优先读入库时登记的 source_file（真正未经处理的源文件）；
    未登记源文件的文档退化为 docstore 节点按章节号拼接，
    并在 source 字段标注 ''file'' / ''nodes''，前端据此提示来源。
    """
    try:
        from kb_pipeline import Manifest

        doc = Manifest().lookup("doc_id", document_id) or {}
        title = doc.get("name") or document_id
        src_file = doc.get("source_file")

        # 1) 源文件原文（真正未经处理）
        if src_file and os.path.isfile(src_file):
            with open(src_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            return {
                "success": True,
                "document_id": document_id,
                "title": title,
                "source": "file",
                "source_file": src_file,
                "char_count": len(text),
                "line_count": text.count("\n") + 1,
                "text": text,
            }

        # 2) 兜底：docstore 节点按章节号还原（已是处理后的文本）
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({"operation": "get_document_nodes", "document_id": document_id})
        sections = result.get("sections") if result.get("success") else None
        if not sections:
            raise HTTPException(status_code=404, detail=result.get("error") or f"文档 {document_id} 不存在或无内容")

        def _sort_key(it):
            no = str(it.get("section_no") or "")
            return [int(p) if p.isdigit() else 0 for p in no.split(".") if p.strip()] or [9999]

        text = "\n\n".join(str(it.get("text") or "") for it in sorted(sections, key=_sort_key))
        return {
            "success": True,
            "document_id": document_id,
            "title": result.get("title") or title,
            "source": "nodes",
            "source_file": None,
            "char_count": len(text),
            "line_count": text.count("\n") + 1,
            "text": text,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get raw document error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/{document_id}")
async def get_knowledge_content(document_id: str):
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'get_content',
            'document_id': document_id
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Get knowledge content error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/preview/{document_id}")
async def preview_knowledge_document(document_id: str):
    """溯源预览页：按章节还原文档结构，供聊天引用链接跳转。"""
    import html as _html
    import re as _re
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({'operation': 'get_document_nodes', 'document_id': document_id})
        sections = result.get('sections') if result.get('success') else None
        if not sections:
            # 兜底：RagFlow 等外部文档不在本地 docstore，用 get_content 整篇渲染
            single = kb_tool.call({'operation': 'get_content', 'document_id': document_id})
            if not single.get('success'):
                raise HTTPException(status_code=404, detail=single.get('error') or result.get('error'))
            sections = [{
                'breadcrumb': '',
                'text': single.get('content') or '',
                'title': single.get('title') or '',
            }]
        result = {'success': True, 'title': (result.get('title') if result.get('success') else '') or sections[0].get('title') or document_id, 'sections': sections}

        img_re = _re.compile(r'&lt;(img\s+src=&quot;)([^&]+)(&quot;[^&]*&gt;)')

        def _render_text(text: str) -> str:
            esc = _html.escape(text or '')
            # 恢复入库时生成的 <img> 标签，其余 HTML 一律转义
            esc = img_re.sub(r'<\1\2\3', esc)
            esc = esc.replace('\n', '<br/>')
            return esc

        sections_html = []
        for it in result.get('sections', []):
            bc = _html.escape(str(it.get('breadcrumb') or ''))
            body = _render_text(it.get('text') or '')
            sections_html.append(
                f'<section class="sec"><div class="bc">{bc}</div>{body}</section>'
            )

        # 清洗标题中的内部文件名杂质：section_5_0015_5.3.8_xxx.txt → 5.3.8 xxx
        import re as _re2
        raw_title = result.get('title') or document_id
        raw_title = _re2.sub(r"^section_\d+_", "", raw_title)
        raw_title = _re2.sub(r"^\d+_(?=\d)", "", raw_title)
        raw_title = _re2.sub(r"\.(txt|md|docx?|pdf)$", "", raw_title, flags=_re2.IGNORECASE)
        raw_title = raw_title.replace('_', ' ')
        title = _html.escape(raw_title)
        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title} · 溯源预览</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f0ede8;margin:0;padding:24px;}}
.wrap{{max-width:860px;margin:0 auto;}}
h1{{font-size:22px;color:#1a1a2e;border-bottom:3px solid #0ea5b7;padding-bottom:12px;}}
.badge{{display:inline-block;background:#e0f2fe;color:#0369a1;font-size:12px;border-radius:10px;padding:2px 10px;vertical-align:middle;margin-left:8px;}}
.sec{{background:#fff;border-radius:10px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.06);line-height:1.75;font-size:14.5px;color:#333;word-break:break-word;}}
.bc{{color:#0ea5b7;font-size:12px;margin-bottom:8px;font-weight:600;}}
img{{max-width:100%;border-radius:6px;margin:6px 0;}}
table{{border-collapse:collapse;margin:8px 0;}} td,th{{border:1px solid #ddd;padding:4px 10px;font-size:13px;}}
.empty{{text-align:center;color:#999;padding:40px;}}
</style></head><body><div class="wrap">
<h1>{title}<span class="badge">知识库溯源</span></h1>
{''.join(sections_html) or '<div class="empty">无内容</div>'}
</div></body></html>"""
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html_doc)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Preview knowledge document error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/knowledge/qa")
async def knowledge_qa(req: KnowledgeQARequest):
    """
    智能问答：KnowledgeQAAgent 四阶段管道
    Stage 1: 查询理解（Qwen 提取实体/属性/时间）
    Stage 2: 多路检索（RagFlow 原问题+关键词+宽泛查询）
    Stage 3: 结构化提取（Qwen 从 chunk 提取 JSON）
    Stage 4: 推理生成（Qwen 合成最终答案 + 来源引用）
    """
    try:
        agent = KnowledgeQAAgent()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, agent.answer, req.question, req.top_k)
        return result
    except Exception as e:
        logger.error(f"Knowledge QA error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.post("/api/knowledge/add")
async def add_knowledge(req: KnowledgeAddRequest):
    """添加文本文档到 RagFlow"""
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'add_document',
            'name': req.name,
            'content': req.content
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Add knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.post("/api/knowledge/upload")
async def upload_knowledge_file(file: UploadFile = File(...)):
    """上传文件到 RagFlow"""
    import tempfile as _tmp
    tmp_path = None
    try:
        # 保存上传文件到临时目录
        suffix = os.path.splitext(file.filename or "document.txt")[1] or ".txt"
        with _tmp.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'upload_file',
            'file_path': tmp_path
        })
        
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Upload knowledge file error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
@router.get("/api/knowledge/diagnose/stats")
async def diagnose_kb_stats():
    """知识库索引诊断统计 - 返回 chunk 数、文档数、存储大小等"""
    try:
        kb_tool = KnowledgeBaseTool()
        if not kb_tool._ensure_initialized():
            return {"success": False, "error": "LlamaIndex 未初始化"}
        
        store = kb_tool._index.docstore
        all_docs = list(store.docs.items())
        real_docs = [(k, v) for k, v in all_docs if k != "placeholder"]
        
        # 按文档分组
        doc_groups = {}
        for doc_id, doc in real_docs:
            meta = doc.metadata or {}
            parent_id = meta.get("document_id", doc_id)
            doc_groups.setdefault(parent_id, []).append((doc_id, doc))
        
        # 详细文档信息
        doc_list = []
        for parent_id, chunks in doc_groups.items():
            first_chunk = chunks[0][1] if chunks else None
            name = first_chunk.metadata.get("title", parent_id[:40]) if first_chunk else parent_id[:40]
            total_chars = sum(len(c.text) for _, c in chunks)
            avg_embedding = sum(len(c.embedding) if c.embedding else 0 for _, c in chunks) / max(len(chunks), 1)
            doc_list.append({
                "id": parent_id,
                "name": name,
                "chunk_count": len(chunks),
                "total_chars": total_chars,
                "avg_embedding_dim": round(avg_embedding),
                "created_at": first_chunk.metadata.get("created_at", "") if first_chunk else ""
            })
        
        doc_list.sort(key=lambda x: x["chunk_count"], reverse=True)
        
        # 存储文件大小
        import glob
        persist_dir = kb_tool._persist_dir
        file_sizes = {}
        for f in glob.glob(os.path.join(persist_dir, "*.json")):
            fname = os.path.basename(f)
            file_sizes[fname] = os.path.getsize(f)
        
        return {
            "success": True,
            "total_chunks": len(real_docs),
            "total_documents": len(doc_groups),
            "persist_dir": persist_dir,
            "chunk_size": kb_tool._index.settings.chunk_size if hasattr(kb_tool._index, 'settings') else 512,
            "chunk_overlap": kb_tool._index.settings.chunk_overlap if hasattr(kb_tool._index, 'settings') else 64,
            "embed_model": kb_tool._embed_model_name,
            "file_sizes": file_sizes,
            "documents": doc_list[:50]  # 最多返回 50 个
        }
    except Exception as e:
        logger.error(f"Diagnose stats error: {e}")
        return {"success": False, "error": str(e)}
@router.delete("/api/knowledge/{kb_id}")
async def delete_knowledge(kb_id: str):
    """删除文档（含其全部切片）。

    失败一律返回 {"success": false, "error": ...} 的 JSON 体，状态码按语义区分：
    404 文档不存在、400 传了切片 id、其余 500，避免前端只能拿到 500 无法区分。
    """
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'delete_document',
            'document_id': kb_id
        })
        if not result.get('success'):
            hint = result.get('hint')
            status = 404 if hint == 'not_found' else (400 if hint == 'chunk_id_given' else 500)
            return JSONResponse(status_code=status, content={
                'success': False,
                'error': result.get('error'),
                'hint': hint,
            })
        return result
    except Exception as e:
        logger.error(f"Delete knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
