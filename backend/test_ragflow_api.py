#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立测试脚本 - 测试 RagFlow API 连接和功能
不依赖 qwen_agent，直接测试 RagFlow API
"""

import os
import requests

# RagFlow 配置
RAGFLOW_API_KEY = "ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
RAGFLOW_API_BASE = "http://172.136.16.14:8080/api/v1"
RAGFLOW_DATASET_ID = "538b0a5c36ff11f18e7d3d43671e73e4"

def test_list_documents():
    """测试列出文档"""
    print("=" * 70)
    print("测试 1: 列出文档列表")
    print("=" * 70)
    
    try:
        url = f"{RAGFLOW_API_BASE}/datasets/{RAGFLOW_DATASET_ID}/documents"
        headers = {"Authorization": f"Bearer {RAGFLOW_API_KEY}"}
        params = {
            "page": 1,
            "page_size": 10,
            "orderby": "create_time",
            "desc": "true"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 0:
            print(f"❌ 错误: {data.get('message')}")
            return False
        
        docs = data.get("data", {}).get("docs", [])
        total = data.get("data", {}).get("total", 0)
        
        print(f"✅ 成功! 文档总数: {total}")
        print(f"\n前 5 个文档:")
        for i, doc in enumerate(docs[:5], 1):
            print(f"  {i}. {doc.get('name')}")
            print(f"     ID: {doc.get('id')}")
            print(f"     状态: {doc.get('run', 'unknown')}")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ 失败: {e}")
        return False


def test_search():
    """测试知识检索"""
    print("=" * 70)
    print("测试 2: 搜索知识")
    print("=" * 70)
    
    try:
        url = f"{RAGFLOW_API_BASE}/retrieval"
        headers = {
            "Authorization": f"Bearer {RAGFLOW_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "question": "超深度开采判定规则",
            "dataset_ids": [RAGFLOW_DATASET_ID],
            "top_k": 3,
            "similarity_threshold": 0.2,
            "vector_similarity_weight": 0.3
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 0:
            print(f"❌ 错误: {data.get('message')}")
            return False
        
        chunks = data.get("data", {}).get("chunks", [])
        
        print(f"✅ 成功! 找到 {len(chunks)} 个结果\n")
        
        for i, chunk in enumerate(chunks, 1):
            print(f"结果 {i}:")
            print(f"  文档: {chunk.get('docnm_kwd', 'N/A')}")
            print(f"  相关度: {chunk.get('similarity', 0):.4f}")
            content = chunk.get('content_with_weight', '')
            print(f"  内容: {content[:150]}...")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ 失败: {e}")
        return False


def test_get_content():
    """测试获取文档内容"""
    print("=" * 70)
    print("测试 3: 获取文档内容")
    print("=" * 70)
    
    try:
        # 先获取文档列表
        url = f"{RAGFLOW_API_BASE}/datasets/{RAGFLOW_DATASET_ID}/documents"
        headers = {"Authorization": f"Bearer {RAGFLOW_API_KEY}"}
        params = {"page": 1, "page_size": 1}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        docs = data.get("data", {}).get("docs", [])
        if not docs:
            print("❌ 没有找到文档")
            return False
        
        doc_id = docs[0].get("id")
        doc_name = docs[0].get("name")
        
        print(f"测试文档: {doc_name}")
        print(f"文档 ID: {doc_id}\n")
        
        # 获取 chunks
        url = f"{RAGFLOW_API_BASE}/datasets/{RAGFLOW_DATASET_ID}/chunks"
        params = {
            "document_id": doc_id,
            "page": 1,
            "page_size": 5
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 0:
            print(f"❌ 错误: {data.get('message')}")
            return False
        
        chunks = data.get("data", {}).get("chunks", [])
        total = data.get("data", {}).get("total", 0)
        
        print(f"✅ 成功! 文档共有 {total} 个 chunk")
        print(f"获取前 {min(5, len(chunks))} 个 chunk:\n")
        
        for i, chunk in enumerate(chunks[:5], 1):
            content = chunk.get('content_with_weight', '')
            print(f"Chunk {i}:")
            print(f"  内容: {content[:200]}...")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ 失败: {e}")
        return False


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("RagFlow API 测试")
    print("=" * 70)
    print(f"API Base: {RAGFLOW_API_BASE}")
    print(f"Dataset ID: {RAGFLOW_DATASET_ID}")
    print("=" * 70 + "\n")
    
    results = []
    
    # 测试 1: 列出文档
    results.append(("列出文档", test_list_documents()))
    
    # 测试 2: 搜索知识
    results.append(("搜索知识", test_search()))
    
    # 测试 3: 获取内容
    results.append(("获取内容", test_get_content()))
    
    # 总结
    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    
    for name, success in results:
        status = "✅ 通过" if success else "❌ 失败"
        print(f"{name}: {status}")
    
    all_passed = all(success for _, success in results)
    print("\n" + ("🎉 所有测试通过!" if all_passed else "⚠️  部分测试失败"))
    print("=" * 70 + "\n")
