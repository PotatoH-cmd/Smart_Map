// chunker.js — 文档处理与预览页的切片规则引擎
//
// 移植自 backend/scripts/ingest_shishifangan_multi.py 的真实切片管线，
// 在浏览器内运行以获得「改规则即时出结果」的实时预览能力。
// 正式入库仍以 Python 脚本为准，本引擎用于调参与效果预演。

export const IMAGE_BASE_URL = 'http://172.136.16.52:82/';

// ─────────────────────────────────────────────
// 默认规则
// ─────────────────────────────────────────────
export const DEFAULT_RULES = {
  // 一、切片结构策略
  sliceMode: 'structural',     // structural=按章节编号层级 | fixed=固定长度滑窗
  breadcrumb: true,            // 输出【章节路径】头
  keepSectionNo: true,         // 路径中保留章节编号
  includeParentIntro: true,    // 附带【上级引言】
  includeChildToc: true,       // 附带【本节子目】

  // 二、长度与拆分
  maxChars: 10000,             // 单块字符上限
  splitOversized: true,        // 超长块按段落/表格边界拆分
  minChars: 80,                // 过短块判定阈值
  mergeShortChunks: false,     // 过短块并入前一块
  chunkSize: 512,              // fixed 模式块大小
  chunkOverlap: 64,            // fixed 模式重叠长度

  // 三、表格与图片
  tableMode: 'keep',           // keep=整块保留 | summary=摘要化 | split=按行拆分
  summaryPerSite: true,        // 综合汇总表按砂场/采区逐条生成独立摘要块
  repeatTableHeader: true,     // 按行拆分时重复表头
  imageMode: 'proxy',          // proxy=加代理前缀 | keep=保留原路径 | strip=剥离

  // 四、文本归一化
  normalizeWhitespace: false,  // 连续空白折叠
  normalizeSectionNo: true,    // 编号内空格/全角点归一化 "5 . 5 . 3" → "5.5.3"
  fullWidthToHalf: false,      // 全角数字/字母转半角
  dropEmptyLines: false,       // 丢弃空文本行
};

export const DEFAULT_SCRIPT = `// ctx = { text, docTitle, rules, chunks }
// chunks 是规则处理后的切片数组，可自由改写后返回。
// 切片字段：chunk_type / section_no / level / breadcrumb / title
//          / has_table / image_paths / text / source_line
// 示例：给含表格的块加检索友好的前缀，并压缩正文空白。

return ctx.chunks.map((c) => {
  const text = c.text.replace(/[ \\t]{2,}/g, ' ');
  if (!c.has_table) return { ...c, text };
  return { ...c, text: '【含数据表】' + '\\n' + text };
});
`;

// ─────────────────────────────────────────────
// 正则（与 Python 脚本一致）
// ─────────────────────────────────────────────
const HEADING_RE = /^(#{1,6})\s*(.*)$/;
const NUM_RE = /^(\d+(?:\.\d+)*)(?:\.\s*|\s+|(?=[^\d\s.]))(.*)$/;
const LIST_HEAD_RE = /^\d+\s*[、）)]/;
const NUM_SPACE_RE = /(?<=\d)\s*[.．]\s*(?=\d)/g;
const TABLE_NAME_RE = /^表\s*\d/;
const IMG_MD_RE = /^!\[\]\((images\/[^)]+)\)\s*(.*)$/;
const CAPTION_RE = /^图\s*\d/;
const IMG_SRC_RE = /(<img\s+src=")(images\/[^"]+)(")/g;
const TABLE_BLOCK_RE = /<table>.*?<\/table>/gs;
const TR_RE = /<tr>.*?<\/tr>/gs;            // 严格 <tr>，用于按行拆表
const TR_LOOSE_RE = /<tr[^>]*>.*?<\/tr>/gs;    // 宽松 <tr ...>，用于摘要抽取
const TABLE_CELL_RE = /<t[hd][^>]*>(.*?)<\/t[hd]>/gs;

// ─────────────────────────────────────────────
// 文本归一化
// ─────────────────────────────────────────────
function toHalfWidth(s) {
  // 仅转换全角数字与字母，避免把中文标点误转成 ASCII
  return s.replace(/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/g,
    (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0));
}

function collapseSpaces(s) {
  return s.replace(/[ \t\u3000]{2,}/g, ' ').trim();
}

function applyTextRules(s, rules) {
  let out = s;
  if (rules.fullWidthToHalf) out = toHalfWidth(out);
  if (rules.normalizeWhitespace) out = collapseSpaces(out);
  return out;
}

function absUrl(path) {
  const p = String(path || '').trim();
  if (/^https?:\/\//.test(p)) return p;
  return IMAGE_BASE_URL + p.replace(/^\/+/, '');
}

function rewriteTableImages(html) {
  const found = [];
  const out = html.replace(IMG_SRC_RE, (_m, pre, path, post) => {
    const full = absUrl(path);
    found.push(full);
    return pre + full + post;
  });
  return { html: out, images: found };
}

// ─────────────────────────────────────────────
// 表格处理
// ─────────────────────────────────────────────
/** 取一行里所有单元格的文本内容。
 * 注意：JS 的 String.match(正则/g) 返回整体匹配，取捕获组必须用 matchAll。 */
function cellValues(rowHtml) {
  return Array.from(rowHtml.matchAll(TABLE_CELL_RE)).map((m) => m[1]);
}

function stripTags(html) {
  return String(html || '').replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').trim();
}

function summarizeTable(html) {
  const rows = html.match(TR_LOOSE_RE) || [];
  const first = rows[0] || '';
  const headers = cellValues(first).map(stripTags).filter(Boolean);
  const sampleIdx = Math.min(1, rows.length - 1);
  const sample = sampleIdx > 0 ? cellValues(rows[sampleIdx]).map(stripTags) : [];
  const parts = [`【表格摘要】共 ${rows.length} 行`];
  if (headers.length) parts.push(`表头：${headers.join(' / ')}`);
  if (sample.length) parts.push(`首行数据：${sample.join(' / ')}`);
  return parts.join('；');
}

function splitTableByRows(html, limit, repeatHeader) {
  const rows = html.match(TR_RE) || [];
  if (rows.length <= 2) return [html];
  const head = repeatHeader ? rows[0] : '';
  const parts = [];
  let cur = repeatHeader ? [head] : [];
  let curLen = head.length;
  for (const r of rows.slice(1)) {
    if (curLen + r.length > limit - 20 && cur.length > (repeatHeader ? 1 : 0)) {
      parts.push('<table>' + cur.join('') + '</table>');
      cur = repeatHeader ? [head] : [];
      curLen = head.length;
    }
    cur.push(r);
    curLen += r.length;
  }
  if (cur.length > (repeatHeader ? 1 : 0)) parts.push('<table>' + cur.join('') + '</table>');
  return parts.length ? parts : [html];
}

// ─────────────────────────────────────────────
// 解析：Markdown 行 → sections
// ─────────────────────────────────────────────
function parseDocument(lines, docTitle, rules) {
  const sections = [];

  const newSection = (kind, num, level, title, heading, line) => {
    const s = { kind, num, level, title, heading, line, elements: [] };
    sections.push(s);
    return s;
  };

  let current = null;
  let pendingTableName = null;
  const n = lines.length;
  let i = 0;

  while (i < n) {
    const line = String(lines[i] || '').replace(/\s+$/, '').trim();
    if (!line) { i += 1; continue; }

    // ---- 标题行 ----
    const m = HEADING_RE.exec(line);
    if (m) {
      const hashes = m[1];
      const htext = m[2].trim();
      const compact = htext.replace(/ /g, '').replace(/\u3000/g, '');
      if (!compact) { i += 1; continue; }

      if (compact === '目录' || compact === '目次') {
        current = newSection('skip', null, 99, compact, line, i + 1);
        i += 1; continue;
      }
      if (LIST_HEAD_RE.test(htext)) {
        if (current && current.kind !== 'skip') current.elements.push({ type: 'subhead', text: htext });
        i += 1; continue;
      }

      const htextNorm = rules.normalizeSectionNo ? htext.replace(NUM_SPACE_RE, '.') : htext;
      let numm = NUM_RE.exec(htextNorm);
      // 首段编号为年份等大数（如 "2025 年度…"）不算章节编号
      if (numm && parseInt(numm[1].split('.')[0], 10) > 30) numm = null;

      if (numm) {
        const num = numm[1];
        const title = numm[2].trim().replace(/\s+/g, ' ');
        const level = num.split('.').length + 1;
        const kind = level === 2 ? 'chapter' : (level === 3 ? 'section' : 'subsection');
        current = newSection(kind, num, level, title, line, i + 1);
      } else if (compact === '前言') {
        current = newSection('preface', null, 2, '前言', line, i + 1);
      } else if (hashes === '#') {
        current = newSection('cover', null, 2, htext, line, i + 1);
      } else if (current && current.kind !== 'skip') {
        current.elements.push({ type: 'subhead', text: htext });
      }
      i += 1; continue;
    }

    // ---- 内容行 ----
    if (!current) current = newSection('cover', null, 2, docTitle, '# ' + docTitle, i + 1);
    if (current.kind === 'skip') { i += 1; continue; }

    if (line.startsWith('<table>')) {
      let tableHtml = line;
      while (!tableHtml.includes('</table>') && i + 1 < n) {
        i += 1;
        tableHtml += String(lines[i] || '').trim();
      }
      const { html, images } = rewriteTableImages(tableHtml);
      current.elements.push({ type: 'table', html, name: pendingTableName, images });
      pendingTableName = null;
      i += 1; continue;
    }

    if (TABLE_NAME_RE.test(line)) {
      let j = i + 1;
      while (j < n && !String(lines[j] || '').trim()) j += 1;
      if (j < n && String(lines[j] || '').trim().startsWith('<table>')) {
        pendingTableName = line;
        i += 1; continue;
      }
    }

    const imgM = IMG_MD_RE.exec(line);
    if (imgM) {
      const path = imgM[1];
      const trailing = imgM[2].trim();
      let caption = CAPTION_RE.test(trailing) ? trailing : '';
      if (!caption) {
        let j = i + 1;
        while (j < n && !String(lines[j] || '').trim()) j += 1;
        if (j < n && CAPTION_RE.test(String(lines[j] || '').trim())) {
          caption = String(lines[j]).trim();
          i = j;
        }
      }
      current.elements.push({ type: 'image', path, caption });
      i += 1; continue;
    }

    if (CAPTION_RE.test(line)) {
      const last = current.elements[current.elements.length - 1];
      if (last && last.type === 'image' && !last.caption) last.caption = line;
      else current.elements.push({ type: 'text', text: line });
      i += 1; continue;
    }

    current.elements.push({ type: 'text', text: line });
    i += 1;
  }

  return sections;
}

// ─────────────────────────────────────────────
// 渲染 elements → 文本
// ─────────────────────────────────────────────
function renderElements(elements, rules) {
  const parts = [];
  const imgPaths = [];
  const tables = [];
  let hasTable = false;

  for (const el of elements) {
    if (el.type === 'text') {
      parts.push(applyTextRules(el.text, rules));
    } else if (el.type === 'subhead') {
      parts.push(`【${applyTextRules(el.text, rules)}】`);
    } else if (el.type === 'table') {
      hasTable = true;
      const name = el.name || '';
      const imgs = el.images || [];
      // 原始 HTML 单独留存：逐站点摘要依赖它，不能依赖渲染后的正文
      tables.push({ html: el.html, name });
      if (rules.tableMode === 'summary') {
        parts.push((name ? name + '\n' : '') + summarizeTable(el.html));
      } else if (rules.tableMode === 'split') {
        const pieces = splitTableByRows(el.html, rules.maxChars, rules.repeatTableHeader);
        parts.push((name ? name + '\n' : '') + pieces.join('\n\n'));
      } else {
        parts.push((name ? name + '\n' : '') + el.html);
      }
      imgPaths.push(...imgs);
    } else if (el.type === 'image') {
      if (rules.imageMode === 'strip') continue;
      const path = rules.imageMode === 'proxy' ? absUrl(el.path) : el.path;
      imgPaths.push(path);
      parts.push(`【图】${el.caption || ''}｜图片地址：${path}`);
    }
  }

  if (rules.dropEmptyLines) {
    for (let k = parts.length - 1; k >= 0; k -= 1) {
      if (!parts[k] || !parts[k].trim()) parts.splice(k, 1);
    }
  }

  return { text: parts.filter((p) => p && p.trim()).join('\n\n'), imgPaths, hasTable, tables };
}

function textOnly(elements, rules) {
  const out = [];
  for (const el of elements) {
    if (el.type === 'text') out.push(applyTextRules(el.text, rules));
    else if (el.type === 'subhead') out.push(`【${applyTextRules(el.text, rules)}】`);
  }
  return out.join('\n').trim();
}

// ─────────────────────────────────────────────
// 综合汇总表 → 逐砂场自然语言摘要
//
// 原始 HTML 表格 embedding 效果极差（标签噪音、数字无语义），且一张汇总表
// 覆盖十几个砂场时 embedding 只代表"平均值"，检索单个站点信号被稀释。
// 因此为每行（每个砂场/采区）单独生成一个摘要块，精准聚焦单一站点。
// ─────────────────────────────────────────────
function isComprehensiveTable(html) {
  if (html.includes('开采点名称') && html.includes('年度采砂控制量')
    && (html.includes('河段长度') || html.includes('采砂船'))) return true;
  if (html.includes('序号') && html.includes('可采区')
    && (html.includes('规划量') || html.includes('开采量')
      || html.includes('控制开采高程') || html.includes('控制开采底高程'))) return true;
  if (html.includes('序号') && html.includes('开采区名称')
    && (html.includes('规划量') || html.includes('开采量') || html.includes('控制开采'))) return true;
  if (html.includes('采区编号') && html.includes('桩号范围')
    && html.includes('控制高程') && html.includes('控制采砂量')) return true;
  return false;
}

/** 从桩号范围推导河段长度（米）：'5+200-6+160' → 960；多段相加。 */
function calcLengthFromStake(stake) {
  let total = 0;
  const re = /(\d+)\+(\d{3})\s*[-~]\s*(\d+)\+(\d{3})/g;
  let m = re.exec(stake);
  while (m) {
    const st = parseInt(m[1], 10) * 1000 + parseInt(m[2], 10);
    const en = parseInt(m[3], 10) * 1000 + parseInt(m[4], 10);
    if (en > st) total += en - st;
    m = re.exec(stake);
  }
  return total > 0 ? total : 0;
}

/** 表头行（前 2 行）跳过，逐行抽成 [(砂场名, 摘要), ...] */
function summarizeTableRowsPerSite(html) {
  if (!isComprehensiveTable(html)) return [];
  const rows = html.match(TR_LOOSE_RE) || [];
  if (rows.length < 3) return [];

  const result = [];
  for (const rowHtml of rows.slice(2)) {
    const cells = cellValues(rowHtml).map((c) => c.trim());
    if (cells.length < 4) continue;

    const name = cells.length > 1 ? cells[1] : '';
    let parts;
    if (name && !/[\u4e00-\u9fa5]{2,}/.test(name)) {
      // 短码格式（如 SH2）：真正的语义名称在 cells[2]（所属区段）
      const locationHint = cells.length > 2 ? cells[2] : '';
      if (!locationHint || !/[\u4e00-\u9fa5]{2,}/.test(locationHint)) continue;
      let locClean = locationHint.replace(/^[^(),，、]+[乡镇](?:街道)?[（(]/, '');
      locClean = locClean.replace(/[)）]$/, '');
      parts = [`${name}（${locClean}）`];
    } else if (!name || name === '合计' || name === '小计' || !/[\u4e00-\u9fa5]/.test(name)) {
      continue;
    } else {
      parts = [name];
    }

    // 河段长度：3-4 位纯数字，后紧跟面积小数
    let lenIdx = -1;
    for (let i = 0; i < cells.length; i += 1) {
      if (/^\d{3,4}$/.test(cells[i])) {
        const n = parseInt(cells[i], 10);
        if (n >= 500 && n <= 4500
          && i + 1 < cells.length && /^\d+\.\d+$/.test(cells[i + 1])) {
          lenIdx = i;
          break;
        }
      }
    }
    if (lenIdx >= 0) {
      parts.push(`河段长度${cells[lenIdx]}米`);
      if (lenIdx + 1 < cells.length && /^\d+\.\d+$/.test(cells[lenIdx + 1])) {
        parts.push(`河段面积${cells[lenIdx + 1]}平方千米`);
      }
    }

    // 行政位置：靠前的中文长文本
    for (const c of cells.slice(2, 9)) {
      if (c.length >= 4 && /[村镇乡社区办事处]/.test(c) && !/[\d+]/.test(c)) {
        parts.push(`位于${c}`);
        break;
      }
    }

    // 计划采砂范围：最靠近河段长度的桩号
    let bestStake = null;
    let bestDist = 999;
    for (let i = 0; i < cells.length; i += 1) {
      if (/^\d+\+[\d]+\s*[-~]\s*\d+\+[\d]+$/.test(cells[i])) {
        if (lenIdx >= 0) {
          const dist = Math.abs(i - lenIdx);
          if (dist < bestDist) { bestDist = dist; bestStake = cells[i]; }
        } else if (bestStake === null) {
          bestStake = cells[i];
        }
      }
    }
    if (bestStake) parts.push(`计划采砂范围${bestStake}`);

    // 年度控制开采高程：长度之后出现的 XX.XX-XX.XX
    let ctrlIdx = -1;
    for (let i = 0; i < cells.length; i += 1) {
      if ((lenIdx < 0 || i > lenIdx) && /^\d+\.\d+\s*[-~]\s*\d+\.\d+$/.test(cells[i])) {
        parts.push(`控制开采高程${cells[i]}米`);
        ctrlIdx = i;
        break;
      }
    }

    // 年度采砂控制量：控制高程之后首个 >1 的小数（排除可利用系数）
    if (ctrlIdx >= 0) {
      for (let j = ctrlIdx + 1; j < Math.min(ctrlIdx + 5, cells.length); j += 1) {
        if (/^\d+\.\d+$/.test(cells[j])) {
          if (parseFloat(cells[j]) > 1.0) {
            parts.push(`年度采砂控制量${cells[j]}万立方米`);
            break;
          }
        }
      }
    }

    // 采砂机具：末尾连续整数
    const nums = [];
    for (let i = cells.length - 1; i >= 0; i -= 1) {
      if (/^\d{1,2}$/.test(cells[i])) nums.push(cells[i]);
      else break;
    }
    nums.reverse();
    const equip = [];
    if (nums.length >= 3) {
      if (nums[nums.length - 3] !== '0') equip.push(`采砂船${nums[nums.length - 3]}艘`);
      if (nums[nums.length - 2] !== '0') equip.push(`提砂船${nums[nums.length - 2]}艘`);
      if (nums[nums.length - 1] !== '0') equip.push(`挖掘机${nums[nums.length - 1]}辆`);
    }
    if (equip.length) parts.push('配备' + equip.join('、'));

    // 储砂场
    for (const c of cells) {
      if (c.endsWith('储砂场')) { parts.push(`储砂场为${c}`); break; }
    }

    result.push([name, parts.join('，')]);
  }
  return result;
}

/** 采区控制指标表（key-value 型），每个采区一条摘要 */
function summarizeCtrlTableRowsPerSite(html) {
  const rows = html.match(TR_LOOSE_RE) || [];
  if (rows.length < 2) return [];

  const KEY_DETAILS = ['桩号范围', '行政区域', '控制高程', '控制采砂量', '控制机具数量', '采区长度', '采区面积'];
  const collected = [];
  let cur = {};
  for (const rowHtml of rows) {
    let cells = cellValues(rowHtml).map((c) => c.trim());
    cells = cells.map((c) => c.replace(/\s+/g, ''));
    // 非 key-value 行（坐标行/合并行）跳过
    if (cells.length !== 2) continue;
    const [key, val] = cells;
    if (!key || !val) continue;
    if (key === '采区编号') {
      if (cur['编号']) collected.push(cur);
      cur = { '编号': val };
    } else if (KEY_DETAILS.includes(key)) {
      cur[key] = val;
    }
  }
  if (cur['编号']) collected.push(cur);

  const summaries = [];
  for (const r of collected) {
    const code = r['编号'] || '';
    const loc = r['行政区域'] || '';
    // 摘要锚点：编号 + 行政区域村庄名（如 SH2（高店乡王湾村）），保证语义检索可命中
    let locClean = loc.replace(/^[^(),，、]+[乡镇](?:街道)?[（(]/, '');
    locClean = locClean.replace(/[)）]$/, '');
    const parts = [locClean && !locClean.includes(code) ? `${code}（${locClean}）` : (code || locClean)];
    if (r['桩号范围']) {
      parts.push(`桩号范围${r['桩号范围']}`);
      const length = calcLengthFromStake(r['桩号范围']);
      if (length) parts.push(`河段长度${length}米`);
    }
    if (loc) parts.push(`位于${loc}`);
    if (r['控制高程']) parts.push(`控制开采高程${r['控制高程']}米`);
    if (r['控制采砂量']) parts.push(`年度采砂控制量${r['控制采砂量']}`);
    if (r['控制机具数量']) parts.push(`采砂机具${r['控制机具数量']}`);
    if (!r['桩号范围']) continue;
    summaries.push([code, parts.join('，')]);
  }
  return summaries;
}

/** 为含综合汇总表的块追加逐站点摘要块 */
function augmentChunksWithTableSummary(chunks) {
  const extra = [];
  const seen = new Set();

  for (const c of chunks) {
    // 用切片携带的原始表格 HTML，而非渲染后正文——否则表格切成摘要化/按行拆分时摘要会静默消失
    for (const t of (c._tables || [])) {
      const tbl = t.html;
      let kind = null;
      let summaries = null;
      if (tbl.includes('采区编号') && tbl.includes('桩号范围')
        && tbl.includes('控制高程') && tbl.includes('控制采砂量')) {
        kind = 'ctrl';
        summaries = summarizeCtrlTableRowsPerSite(tbl);
      } else if (isComprehensiveTable(tbl)) {
        kind = 'sum';
        summaries = summarizeTableRowsPerSite(tbl);
      } else {
        continue;
      }
      if (!summaries || !summaries.length) continue;
      c.has_table = true;
      const docTitle = (c.breadcrumb || '').split(' > ')[0] || '';
      for (const [siteName, siteSummary] of summaries) {
        const key = `${c.section_no}|${c.breadcrumb}|${kind}|${siteName}`;
        if (seen.has(key)) continue;
        seen.add(key);
        extra.push({
          chunk_type: 'summary',
          section_no: c.section_no,
          level: c.level,
          breadcrumb: `${c.breadcrumb} > 综合汇总表 > ${siteName}`,
          title: `${c.title}｜${siteName}`,
          has_table: false,
          image_paths: [],
          text: `【文档】${docTitle}\n【章节】综合汇总表 - ${siteName}\n\n${siteSummary}`,
          source_line: c.source_line,
        });
      }
    }
  }

  return chunks.concat(extra);
}

// ─────────────────────────────────────────────
// 结构切片
// ─────────────────────────────────────────────
function buildStructuralChunks(sections, docTitle, rules) {
  const numMap = new Map();
  const allNums = [];
  sections.forEach((s) => {
    if (s.num) { numMap.set(s.num, s); allNums.push(s.num); }
  });

  const hasChildren = (num) => {
    const prefix = num + '.';
    return allNums.some((x) => x.startsWith(prefix));
  };

  const ancestors = (num) => {
    const segs = num.split('.');
    const out = [];
    for (let k = 1; k < segs.length; k += 1) out.push(segs.slice(0, k).join('.'));
    return out;
  };

  const label = (num, title) => (rules.keepSectionNo ? `${num} ${title}` : title);

  const chunks = [];

  const emit = (sec, chunkType, text, breadcrumb, extraImgs, hasTable, tables) => {
    const t = (text || '').trim();
    if (!t) return;
    const imagePaths = [...new Set(extraImgs || [])];
    chunks.push({
      chunk_type: chunkType,
      section_no: sec.num || '',
      level: sec.level || 0,
      breadcrumb,
      title: breadcrumb ? breadcrumb.split(' > ').pop() : sec.title,
      has_table: !!hasTable,
      image_paths: imagePaths,
      _tables: tables || [],
      text: t,
      source_line: sec.line,
    });
  };

  for (const sec of sections) {
    const kind = sec.kind;
    if (kind === 'skip') continue;

    if (kind === 'cover' || kind === 'preface') {
      const lbl = kind === 'cover' ? '封面与批复意见' : '前言';
      const { text: body, imgPaths, hasTable, tables } = renderElements(sec.elements, rules);
      const line = `【文档】${docTitle}\n【章节】${lbl}`;
      emit(sec, kind, line + '\n\n' + body, `${docTitle} > ${lbl}`, imgPaths, hasTable, tables);
      continue;
    }

    if (kind === 'chapter') {
      if (hasChildren(sec.num)) continue;
      const { text: body, imgPaths, hasTable, tables } = renderElements(sec.elements, rules);
      const bc = `${docTitle} > ${label(sec.num, sec.title)}`;
      const line = `【文档】${docTitle}\n【章节路径】${label(sec.num, sec.title)}`;
      emit(sec, 'parent', line + '\n\n' + body, bc, imgPaths, hasTable, tables);
      continue;
    }

    if (kind === 'section') {
      const chapNum = sec.num.split('.')[0];
      const chap = numMap.get(chapNum);
      const chapTitle = chap ? label(chap.num, chap.title) : chapNum;
      const bc = `${docTitle} > ${chapTitle} > ${label(sec.num, sec.title)}`;
      const chapLead = chap ? textOnly(chap.elements, rules) : '';
      const { text: ownBody, imgPaths, hasTable, tables } = renderElements(sec.elements, rules);
      const childTitles = allNums
        .filter((x) => x.startsWith(sec.num + '.')
          && x.split('.').length === sec.num.split('.').length + 1)
        .map((x) => label(numMap.get(x).num, numMap.get(x).title));

      const head = rules.breadcrumb
        ? `【文档】${docTitle}\n【章节路径】${chapTitle} > ${label(sec.num, sec.title)}`
        : `【文档】${docTitle}`;
      const bodyParts = [head];
      if (rules.includeParentIntro && chapLead) bodyParts.push(`【上级引言】${chapLead}`);
      if (ownBody) bodyParts.push(ownBody);
      if (rules.includeChildToc && childTitles.length) bodyParts.push(`【本节子目】${childTitles.join('；')}`);
      emit(sec, 'parent', bodyParts.join('\n\n'), bc, imgPaths, hasTable, tables);
      continue;
    }

    if (kind === 'subsection') {
      const anc = ancestors(sec.num);
      const bcParts = [docTitle];
      const leadParts = [];
      for (const a of anc) {
        const asec = numMap.get(a);
        if (asec) {
          bcParts.push(label(asec.num, asec.title));
          const lead = textOnly(asec.elements, rules);
          if (lead) leadParts.push(`${label(asec.num, asec.title)}：${lead}`);
        } else {
          bcParts.push(a);
        }
      }
      bcParts.push(label(sec.num, sec.title));
      const breadcrumb = bcParts.join(' > ');
      const { text: ownBody, imgPaths, hasTable, tables } = renderElements(sec.elements, rules);

      const head = rules.breadcrumb
        ? `【文档】${docTitle}\n【章节路径】${bcParts.slice(1).join(' > ')}`
        : `【文档】${docTitle}`;
      const bodyParts = [head];
      if (rules.includeParentIntro && leadParts.length) {
        bodyParts.push('【上级引言】\n' + leadParts.join('\n'));
      }
      if (ownBody) bodyParts.push(ownBody);
      emit(sec, 'child', bodyParts.join('\n\n'), breadcrumb, imgPaths, hasTable, tables);
    }
  }

  return chunks;
}

// ─────────────────────────────────────────────
// 超长块拆分
// ─────────────────────────────────────────────
function splitOversized(chunks, docTitle, rules) {
  const maxChars = rules.maxChars;
  const out = [];

  for (const c of chunks) {
    const text = c.text;
    if (text.length <= maxChars) { out.push(c); continue; }

    const blocks = text.split('\n\n');
    const hasHeader = blocks[0] && blocks[0].startsWith('【文档】');
    const header = hasHeader ? blocks[0] : `【文档】${docTitle}`;
    const body = hasHeader ? blocks.slice(1).join('\n\n') : text;

    // 按表格边界切成原子段：表格整块，其余按段落
    const segments = [];
    let pos = 0;
    TABLE_BLOCK_RE.lastIndex = 0;
    let tm = TABLE_BLOCK_RE.exec(body);
    while (tm) {
      const before = body.slice(pos, tm.index).trim();
      if (before) segments.push(...before.split('\n\n').filter((s) => s.trim()));
      const tbl = tm[0];
      if (tbl.length > maxChars - header.length - 100) {
        segments.push(...splitTableByRows(tbl, maxChars - header.length - 100, rules.repeatTableHeader));
      } else {
        segments.push(tbl);
      }
      pos = tm.index + tbl.length;
      tm = TABLE_BLOCK_RE.exec(body);
    }
    const tail = body.slice(pos).trim();
    if (tail) segments.push(...tail.split('\n\n').filter((s) => s.trim()));

    // 贪心合并
    const parts = [];
    let cur = [];
    let curLen = 0;
    const budget = maxChars - header.length - 100;
    for (const seg of segments) {
      if (cur.length && curLen + seg.length > budget) {
        parts.push(cur.join('\n\n'));
        cur = [];
        curLen = 0;
      }
      cur.push(seg);
      curLen += seg.length + 2;
    }
    if (cur.length) parts.push(cur.join('\n\n'));

    const total = parts.length || 1;
    const partTables = parts.map((ptext) => (c._tables || []).filter((t) => ptext.includes(t.html)));
    if (partTables.length && partTables.every((arr) => arr.length === 0)) {
      partTables[0].push(...(c._tables || []));
    }
    parts.forEach((ptext, k) => {
      const suffix = `（第${k + 1}/${total}部分）`;
      out.push({
        ...c,
        text: `${header}${suffix}\n\n${ptext}`,
        title: c.title + suffix,
        breadcrumb: c.breadcrumb + suffix,
        has_table: ptext.includes('<table>'),
        image_paths: c.image_paths.filter((u) => ptext.includes(u)),
        _tables: partTables[k] || [],
        part: k + 1,
        part_total: total,
      });
    });
  }

  return out;
}

// ─────────────────────────────────────────────
// 过短块合并
// ─────────────────────────────────────────────
function mergeShortChunks(chunks, rules) {
  const maxChars = rules.maxChars;
  const out = [];
  for (const c of chunks) {
    const prev = out[out.length - 1];
    if (prev && c.text.length < rules.minChars
      && prev.text.length + c.text.length + 2 <= maxChars) {
      prev.text = prev.text + '\n\n' + c.text;
      prev.merged = (prev.merged || 0) + 1;
      prev.has_table = prev.has_table || c.has_table;
      prev.image_paths = [...new Set([...prev.image_paths, ...c.image_paths])];
      continue;
    }
    out.push({ ...c, image_paths: [...c.image_paths] });
  }
  return out;
}

// ─────────────────────────────────────────────
// 固定长度切片
// ─────────────────────────────────────────────
function buildFixedChunks(text, docTitle, rules) {
  const size = Math.max(64, Number(rules.chunkSize) || 512);
  const overlap = Math.max(0, Math.min(Number(rules.chunkOverlap) || 0, size - 1));
  let body = text;
  if (rules.fullWidthToHalf) body = toHalfWidth(body);
  if (rules.normalizeWhitespace) body = collapseSpaces(body);

  const sentences = body.split(/(?<=[。！？;.!?\n])/);
  const chunks = [];
  let cur = '';
  for (const s of sentences) {
    if (cur.length + s.length <= size) {
      cur += s;
    } else {
      if (cur.trim()) chunks.push(cur.trim());
      const tail = cur.length >= overlap ? cur.slice(-overlap) : cur;
      cur = tail + s;
    }
  }
  if (cur.trim()) chunks.push(cur.trim());

  return chunks.map((t, i) => ({
    chunk_type: 'fixed',
    section_no: '',
    level: 0,
    breadcrumb: docTitle,
    title: `${docTitle}（第${i + 1}块）`,
    has_table: t.includes('<table>'),
    image_paths: [],
    text: t,
    source_line: 0,
  }));
}

// ─────────────────────────────────────────────
// 预览渲染：把切片正文切成「文本段 / 表格段」
// ─────────────────────────────────────────────
const CELL_SPAN_RE = /<t[hd]([^>]*)>(.*?)<\/t[hd]>/gs;

function parseTableRows(html) {
  return (html.match(TR_LOOSE_RE) || []).map((rowHtml) => (
    Array.from(rowHtml.matchAll(CELL_SPAN_RE)).map((m) => {
      const attrs = m[1] || '';
      const cs = /colspan\s*=\s*"?(\d+)"?/i.exec(attrs);
      return {
        text: stripTags(m[2]),
        colspan: cs ? Math.min(parseInt(cs[1], 10) || 1, 12) : 1,
      };
    })
  ));
}

/** 切片正文 → segments：[{type:'text',text} | {type:'table',rows}] */
export function parseChunkSegments(text) {
  const segs = [];
  const src = text || '';
  let pos = 0;
  TABLE_BLOCK_RE.lastIndex = 0;
  let m = TABLE_BLOCK_RE.exec(src);
  while (m) {
    if (m.index > pos) segs.push({ type: 'text', text: src.slice(pos, m.index) });
    segs.push({ type: 'table', rows: parseTableRows(m[0]) });
    pos = m.index + m[0].length;
    m = TABLE_BLOCK_RE.exec(src);
  }
  if (pos < src.length) segs.push({ type: 'text', text: src.slice(pos) });
  return segs;
}

// ─────────────────────────────────────────────
// 统计
// ─────────────────────────────────────────────
export function summarize(chunks, rules) {
  const sizes = chunks.map((c) => c.text.length);
  const total = sizes.reduce((a, b) => a + b, 0);
  const max = sizes.length ? Math.max(...sizes) : 0;
  const min = sizes.length ? Math.min(...sizes) : 0;
  return {
    count: chunks.length,
    totalChars: total,
    avgChars: sizes.length ? Math.round(total / sizes.length) : 0,
    maxChars: max,
    minChars: min,
    oversized: sizes.filter((s) => s > rules.maxChars).length,
    undersized: sizes.filter((s) => s < rules.minChars).length,
    tableChunks: chunks.filter((c) => c.has_table).length,
    images: chunks.reduce((a, c) => a + (c.image_paths || []).length, 0),
    parents: chunks.filter((c) => ['parent', 'cover', 'preface'].includes(c.chunk_type)).length,
    children: chunks.filter((c) => c.chunk_type === 'child').length,
    summaries: chunks.filter((c) => c.chunk_type === 'summary').length,
  };
}

// ─────────────────────────────────────────────
// 脚本执行（前端沙箱）
// ─────────────────────────────────────────────
const ALLOWED_FIELDS = [
  'chunk_type', 'section_no', 'level', 'breadcrumb', 'title',
  'has_table', 'image_paths', 'text', 'source_line',
];

function sanitizeChunks(raw) {
  if (!Array.isArray(raw)) throw new Error('脚本必须返回 chunks 数组');
  return raw.map((c, i) => {
    if (!c || typeof c !== 'object') throw new Error(`第 ${i + 1} 项不是对象`);
    if (typeof c.text !== 'string') throw new Error(`第 ${i + 1} 项缺少 text 字段`);
    const out = {};
    ALLOWED_FIELDS.forEach((k) => {
      if (c[k] !== undefined) out[k] = c[k];
    });
    out.chunk_type = out.chunk_type || 'custom';
    out.title = out.title || `自定义块 ${i + 1}`;
    out.breadcrumb = out.breadcrumb || '';
    out.section_no = out.section_no || '';
    out.image_paths = Array.isArray(out.image_paths) ? out.image_paths : [];
    out.has_table = !!out.has_table;
    return out;
  });
}

// new Function 生成的函数体首行 = 用户脚本第 1 行，但 V8 的报错行号
// 是从 function 声明算起的，固定相差 2 行（参数行 + 左花括号行）。
const NEW_FUNCTION_LINE_OFFSET = 2;

// 从错误堆栈里换算用户脚本的行号；语法错误在 V8 下不带行号，返回 0 表示未知
function resolveErrorLine(stack) {
  if (!stack) return 0;
  const m = /<anonymous>:(\d+):\d+/.exec(stack) || /Function:(\d+):\d+/.exec(stack);
  if (!m) return 0;
  const line = Number(m[1]) - NEW_FUNCTION_LINE_OFFSET;
  return line > 0 ? line : 0;
}

export function runScript(script, ctx) {
  const t0 = performance.now();
  try {
    // eslint-disable-next-line no-new-func
    const fn = new Function('ctx', script);
    const result = fn(ctx);
    const chunks = sanitizeChunks(result);
    return { chunks, error: null, errorLine: 0, ms: Math.round(performance.now() - t0) };
  } catch (e) {
    return {
      chunks: null,
      error: e && e.message ? e.message : String(e),
      errorLine: resolveErrorLine(e && e.stack),
      ms: Math.round(performance.now() - t0),
    };
  }
}

// ─────────────────────────────────────────────
// 主入口
// ─────────────────────────────────────────────
export function processDocument({ rawText, docTitle, rules, script }) {
  const t0 = performance.now();
  const text = rawText || '';
  const title = docTitle || '未命名文档';

  let chunks;
  if (rules.sliceMode === 'fixed') {
    chunks = buildFixedChunks(text, title, rules);
  } else {
    const lines = text.split('\n');
    const sections = parseDocument(lines, title, rules);
    chunks = buildStructuralChunks(sections, title, rules);
    if (rules.summaryPerSite) chunks = augmentChunksWithTableSummary(chunks);
    if (rules.splitOversized) chunks = splitOversized(chunks, title, rules);
    if (rules.mergeShortChunks) chunks = mergeShortChunks(chunks, rules);
  }

  // _tables 是内部字段（原始表格 HTML），不对外暴露给脚本与 UI
  chunks = chunks.map((c) => {
    if (!c._tables) return c;
    const { _tables, ...rest } = c;
    return rest;
  });

  const baseStats = summarize(chunks, rules);
  const ruleMs = Math.round(performance.now() - t0);

  let scriptError = null;
  let scriptErrorLine = 0;
  let scriptMs = 0;
  const trimmed = (script || '').trim();
  if (trimmed) {
    const r = runScript(script, { text, docTitle: title, rules, chunks });
    scriptMs = r.ms;
    if (r.error) {
      scriptError = r.error;
      scriptErrorLine = r.errorLine || 0;
    } else {
      chunks = r.chunks;
    }
  }

  return {
    chunks,
    stats: summarize(chunks, rules),
    baseStats,
    ruleMs,
    scriptMs,
    scriptError,
    scriptErrorLine,
  };
}