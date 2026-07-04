import re

def generate_structured_md(input_path, output_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    regions = ["浉河区", "平桥区", "罗山县", "潢川县", "固始县", "息县", "淮滨县", "光山县", "商城县", "新县"]
    
    # 初始化状态
    new_content = []
    current_chapter = ""
    current_section = ""
    in_header_zone = True  # 是否处于封面/目录区
    in_table = False
    table_buffer = []

    # 定义正则
    re_chapter = re.compile(r'^#?\s*([一二三四五六七八九十]+、)\s*(.*)')
    re_section = re.compile(r'^#?\s*(\d+\.\d+)\s*(.*)')
    re_subsection = re.compile(r'^#?\s*（([一二三四五六七八九十])）\s*(.*)')
    re_noise = re.compile(r'信阳市水利局.*总结报告')

    for line in lines:
        clean_line = line.strip()

        # 1. 过滤页眉噪声
        if re_noise.search(clean_line):
            continue

        # 2. 跳过封面和目录，直到看到第一章
        if in_header_zone:
            if "一、" in clean_line and "项目概况" in clean_line:
                in_header_zone = False
            else:
                continue

        # 3. 处理 HTML 表格转换 (简单正则法)
        if "<table" in clean_line:
            in_table = True
            table_buffer = []
            continue
        if "</table>" in clean_line:
            in_table = False
            new_content.append("\n> [表格说明]: 见下表数据\n")
            # 这里可以调用处理函数把 buffer 转成 MD，此处简化为标记
            new_content.append("| 编号 | 数据项 | 内容 | 状态 |\n|---|---|---|---|\n") 
            continue
        if in_table:
            # 提取 td 里的内容简单模拟
            td_content = re.findall(r'<td>(.*?)</td>', clean_line)
            if td_content:
                table_buffer.append(f"| {' | '.join(td_content)} |")
            continue

        # 4. 识别并转换层级 + 注入元数据
        # 一级标题
        chapter_match = re_chapter.match(clean_line)
        if chapter_match:
            current_chapter = chapter_match.group(2)
            new_content.append(f"\n# {chapter_match.group(1)} {current_chapter}")
            new_content.append(f"> **[Metadata] Level: Chapter | Topic: {current_chapter}**\n")
            continue

        # 二级标题
        section_match = re_section.match(clean_line)
        if section_match:
            current_section = section_match.group(2)
            found_region = next((r for r in regions if r in current_section), "全市/通用")
            new_content.append(f"\n## {section_match.group(1)} {current_section}")
            new_content.append(f"> **[Metadata] Level: Section | Region: {found_region}**\n")
            continue

        # 三级标题
        sub_match = re_subsection.match(clean_line)
        if sub_match:
            new_content.append(f"\n### （{sub_match.group(1)}）{sub_match.group(2)}")
            continue

        # 5. 普通文本处理
        if clean_line:
            new_content.append(clean_line)
        else:
            new_content.append("")

    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(new_content))

    print(f"成功生成预览文件: {output_path}")

# 执行
if __name__ == "__main__":
    generate_structured_md('full.md', 'full_structured.md')