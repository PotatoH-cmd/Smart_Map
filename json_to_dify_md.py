import json
import os
import shutil
import re
from pathlib import Path

# ========= 配置路径 =========
JSON_PATH = "/home/server/python/map_assistant_v1/cleaned_rag_data.json"
IMAGE_SRC_DIR = "/home/server/python/map_assistant_v1/images"
OUTPUT_DIR = "/home/server/python/map_assistant_v1/dify_md_output"

# ========= 初始化 =========
os.makedirs(OUTPUT_DIR, exist_ok=True)
OUTPUT_IMAGE_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(OUTPUT_IMAGE_DIR, exist_ok=True)

# ========= 读取 JSON =========
with open(JSON_PATH, "r", encoding="utf-8") as f:
    chunks = json.load(f)

print(f"📦 读取 chunk 数量: {len(chunks)}")

# ========= 工具函数 =========
def extract_images(markdown_text):
    """
    提取 markdown 中的图片路径
    ![](images/xxx.jpg)
    """
    pattern = r"!\[.*?\]\((images/[^)]+)\)"
    return re.findall(pattern, markdown_text)

def clean_heading_hash(text):
    """
    清理 content 中多余的 '#'（防止破坏 md 结构）
    """
    return re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)

# ========= 主处理流程 =========
for chunk in chunks:
    chunk_id = chunk.get("id")
    if not chunk_id:
        continue

    content = chunk.get("content", "").strip()
    if not content:
        continue

    # 清理多余 #
    content = clean_heading_hash(content)

    # -------- Front Matter --------
    front_matter = {
        "chunk_id": chunk.get("id"),
        "chapter": chunk.get("chapter"),
        "section": chunk.get("section"),
        "subsection": chunk.get("subsection"),
        "region": chunk.get("region"),
        "river": chunk.get("river"),
        "type": chunk.get("type"),
    }

    fm_lines = ["---"]
    for k, v in front_matter.items():
        if v:
            fm_lines.append(f"{k}: {str(v).strip()}")
    fm_lines.append("---\n")

    # -------- 写 MD 文件 --------
    md_path = os.path.join(OUTPUT_DIR, f"{chunk_id}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(fm_lines))
        f.write(content)
        f.write("\n")

    # -------- 拷贝图片 --------
    image_paths = extract_images(content)
    for img_rel_path in image_paths:
        img_name = os.path.basename(img_rel_path)
        src_img = os.path.join(IMAGE_SRC_DIR, img_name)
        dst_img = os.path.join(OUTPUT_IMAGE_DIR, img_name)

        if os.path.exists(src_img) and not os.path.exists(dst_img):
            shutil.copy2(src_img, dst_img)

print("✅ 转换完成：Markdown + images 已生成")
print(f"📁 输出目录: {OUTPUT_DIR}")
