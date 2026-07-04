import re
import json

class RAGDataProcessor:
    def __init__(self):
        # 1. 配置标签库
        self.regions = ["浉河区", "平桥区", "罗山县", "光山县", "新县", "商城县", "固始县", "潢川县", "淮滨县", "息县", "信阳市"]
        self.rivers = ["淮河", "潢河", "白露河", "史灌河", "灌河", "史河", "浉河", "竹竿河", "随河"]
        
        # 2. 正则表达式定义
        self.re_l1 = re.compile(r'^(?:#\s*)?([一二三四五六七八九十]+、\s*.+)')
        self.re_l2 = re.compile(r'^(?:#\s*)?(\d+\.\d+\s+[^0-9\s].+)') # 排除以纯数字(如坐标)开头的干扰行
        self.re_l3 = re.compile(r'^(?:#\s*)?(\d+(\.\d+){2,}\s+.+)')  # 匹配 5.6.12.1 这种细粒度层级
        
        # 3. 状态维护
        self.chunks = []
        self.buffer = ""
        self.ctx = {
            "chapter": "",
            "section": "",
            "subsection": "",
            "last_river": "未知河流",
            "last_sub_prefix": "" # 用于河流标签传播
        }

    def clean_text(self, text):
        """修复 OCR 噪声、合并断裂公式、清理 HTML"""
        # 合并断裂的数学单位 $ 4 2 . 0 \mathrm { m } $ -> 42.0m
        text = re.sub(r'\$\s*([\d\.\s]+)\s*\\mathrm\s*\{\s*([a-zA-Z]+)\s*\}\s*\$', 
                      lambda m: m.group(1).replace(" ", "") + m.group(2), text)
        # 修复范围符 $ 4 7 . 5 { \sim } 5 0 . 5 \mathrm { m } $
        text = re.sub(r'\{\s*\\sim\s*\}', '~', text)
        # 清理 HTML 表格标签
        text = re.sub(r'</?(td|tr|table|tbody|th)[^>]*>', ' ', text)
        text = re.sub(r'<[^>]+>', '', text)
        # 移除多余空格
        return re.sub(r'\s+', ' ', text).strip()

    def extract_tags(self, content):
        """智能提取行政区和河流，支持向下传播"""
        combined = f"{self.ctx['section']} {self.ctx['subsection']} {content}"
        
        # 提取行政区
        region = next((r for r in self.regions if r in combined), "信阳市")
        
        # 提取河流并处理传播逻辑
        current_river = next((rv for rv in self.rivers if rv in combined), None)
        
        # 获取当前三级编号前缀 (例如 5.6.12.1 -> 5.6.12)
        match = re.match(r'(\d+\.\d+\.\d+)', self.ctx['subsection'])
        current_prefix = match.group(1) if match else ""

        if current_river:
            self.ctx['last_river'] = current_river
            self.ctx['last_sub_prefix'] = current_prefix
        else:
            # 如果前缀一致，则继承上一块的河流
            if current_prefix != self.ctx['last_sub_prefix']:
                self.ctx['last_river'] = "未知河流"
        
        return region, self.ctx['last_river']

    def flush(self):
        """将 buffer 内容压入 chunks"""
        content = self.clean_text(self.buffer)
        if not content or len(content) < 5: # 过滤极短噪声
            self.buffer = ""
            return

        region, river = self.extract_tags(content)
        
        # 再次检查 section 是否被采砂参数污染
        section_name = self.ctx['section']
        if "万" in section_name or "吨" in section_name:
             section_name = "采砂区监测详细数据" # 降噪修正

        self.chunks.append({
            "id": f"chunk_{len(self.chunks) + 1:04d}",
            "content": content,
            "chapter": self.ctx['chapter'],
            "section": section_name,
            "subsection": self.ctx['subsection'],
            "region": region,
            "river": river,
            "type": "paragraph"
        })
        self.buffer = ""

    def post_process_merge(self):
        """后处理：合并孤立的图片碎片块"""
        if not self.chunks: return []
        merged = [self.chunks[0]]
        
        for i in range(1, len(self.chunks)):
            curr, prev = self.chunks[i], merged[-1]
            
            # 判断条件：相同小节且当前块主要是图片或极短图注
            is_same_sub = curr['subsection'] == prev['subsection']
            is_fragment = ("![" in curr['content'] or "图 " in curr['content']) and len(curr['content']) < 300
            
            if is_same_sub and is_fragment:
                prev['content'] += "\n" + curr['content']
            else:
                merged.append(curr)
        return merged

    def run(self, input_file):
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        start_flag = False
        for line in lines:
            line = line.strip()
            if not line: continue

            # 1. 非正文剥离：直到遇到第一章才开始
            if not start_flag:
                if self.re_l1.match(line): start_flag = True
                else: continue

            # 2. 标题层级感应
            if self.re_l1.match(line):
                self.flush()
                self.ctx['chapter'] = re.sub(r'^#+\s*', '', line)
                self.ctx['section'], self.ctx['subsection'] = "", ""
            elif self.re_l2.match(line):
                self.flush()
                self.ctx['section'] = re.sub(r'^#+\s*', '', line)
                self.ctx['subsection'] = ""
            elif self.re_l3.match(line):
                self.flush()
                self.ctx['subsection'] = re.sub(r'^#+\s*', '', line)
            else:
                # 3. 内容累加 (1)(2)(3) 自动进入同一个 buffer
                self.buffer += line + "\n"

        self.flush()
        # 4. 执行碎片合并
        return self.post_process_merge()

# --- 使用说明 ---
if __name__ == "__main__":
    processor = RAGDataProcessor()
    # 假设您的输入文件是 full.md
    final_data = processor.run("full.md")
    
    # 输出 JSON
    with open("cleaned_rag_data.json", "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
    
    print(f"清洗成功！生成的 JSON 已保存。总计 Chunk 数：{len(final_data)}")