
import sys
import os
import json
import logging

# 添加 backend 目录到路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend')))

from tools.knowledge_base_tool import KnowledgeBaseTool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_hierarchical_indexing():
    kb = KnowledgeBaseTool()
    
    # 1. 添加一个长文本
    print("\n--- 测试添加长文本并自动分块 ---")
    long_text = """
    采砂管理规定：
    第一条 为了加强采砂管理，保障河道安全，制定本规定。
    第二条 严禁在桥梁上下游各500米范围内采砂。
    第三条 采砂深度不得超过许可设计的最大深度（h_max）。
    第四条 采砂作业时间为每日 08:00 至 18:00，夜间禁止施工。
    第五条 发现超采行为，将处以5万元以上10万元以下罚款。
    """
    res = kb.call({
        'operation': 'add',
        'title': '河道采砂管理细则',
        'content': long_text,
        'tags': ['管理', '法规']
    })
    print(json.dumps(res, indent=2, ensure_ascii=False))

    # 2. 测试通过其中一小段话检索出整篇内容
    print("\n--- 测试检索: '桥梁附近能采砂吗？' ---")
    res = kb.call({
        'operation': 'search',
        'query': '桥梁附近能采砂吗？'
    })
    print(json.dumps(res, indent=2, ensure_ascii=False))

    # 3. 列出所有知识点 (应该只看到父节点)
    print("\n--- 测试列出所有知识点 (不应看到分块) ---")
    res = kb.call({
        'operation': 'list_all'
    })
    for item in res.get('data', []):
        print(f"ID: {item['id']}, Title: {item['title']}")

if __name__ == "__main__":
    test_hierarchical_indexing()
