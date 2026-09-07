"""知识库：文档检索/问答/上传/图谱（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from tools.knowledge_graph_tool import get_kg
import asyncio
from tools.knowledge_qa_agent import KnowledgeQAAgent
import os
# 知识库后端选择（环境变量 KNOWLEDGE_BACKEND: ragflow|llamaindex，默认 ragflow）— 与 main.py 保持一致
import os as _os
_KB_BACKEND = _os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
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
    """列出知识库项目文件夹及各自文档数（供前端“选择文件夹”入口）。"""
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({'operation': 'list_folders'})
        # 兼容不支持文件夹的后端（如 RagFlow）：返回空列表而非报错
        if not result.get('success'):
            return {'success': True, 'folders': [], 'total_folders': 0, 'total_documents': 0}
        return result
    except Exception as e:
        logger.error(f"List knowledge folders error: {e}")
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
    """从 RagFlow 删除文档"""
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'delete_document',
            'document_id': kb_id
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Delete knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
