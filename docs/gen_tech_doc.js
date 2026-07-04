const fs = require('fs');
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, BorderStyle, WidthType, ShadingType,
  LevelFormat, Header, Footer, PageNumber } = require('docx');

const B = { style: BorderStyle.SINGLE, size: 1, color: 'CCCCCC' };
const Bs = { top: B, bottom: B, left: B, right: B };

function H1(t) { return new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] }); }
function H2(t) { return new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] }); }
function P(t) { return new Paragraph({ spacing: { after: 80 }, children: [new TextRun(t)] }); }
function Code(t) { return new Paragraph({ shading: { fill: 'F5F5F5', type: ShadingType.CLEAR }, indent: { left: 360 }, children: [new TextRun({ text: t, font: 'Consolas', size: 18 })] }); }
function Bullet(t) { return new Paragraph({ numbering: { reference: 'b', level: 0 }, children: [new TextRun(t)] }); }
function Num(t) { return new Paragraph({ numbering: { reference: 'n', level: 0 }, children: [new TextRun(t)] }); }

function Cell(text, w, shade=false) {
  return new TableCell({
    borders: Bs, width: { size: w, type: WidthType.DXA },
    shading: shade ? { fill: 'EBF4FF', type: ShadingType.CLEAR } : undefined,
    children: [P(text)]
  });
}
function Row(cells, widths, header=false) {
  return new TableRow({ children: cells.map((t,i) => Cell(t, widths[i], header)) });
}

const doc = new Document({
  numbering: {
    config: [
      { reference: 'b', levels: [{ level: 0, format: LevelFormat.BULLET, text: '\u2022', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: 'n', levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ]
  },
  styles: {
    default: { document: { run: { font: 'Arial', size: 22 } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 36, bold: true, color: '1a365d' }, spacing: { before: 300, after: 200 }, outlineLevel: 0 },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 28, bold: true, color: '2c5282' }, spacing: { before: 240, after: 160 }, outlineLevel: 1 },
    ]
  },
  sections: [{
    properties: { page: { margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: 'Map Assistant v1 - SAM Target Recognition Technical Doc', size: 18, color: '888' })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun('Page '), new TextRun({ children: [PageNumber.CURRENT] })] })] }) },
    children: [

H1('Map Assistant v1 \u2014 SAM \u76ee\u6807\u8bc6\u522b\u6280\u672f\u6587\u6863'),
P('\u65e5\u671f\uff1a2026-04-18 | \u7248\u672c\uff1aSegEarth-OV-3 / SAM3 \u6587\u672c\u63d0\u793a\u8bed\u4e49\u5206\u5272\u7cfb\u7edf'),

// === 1 ===
H1('1. \u9879\u76ee\u6982\u8ff0'),
P('SAM \u76ee\u6807\u8bc6\u522b\u662f Map Assistant v1 \u7684\u6838\u5fc3\u529f\u80fd\uff0c\u7528\u6237\u5728\u536b\u661f\u5f71\u50cf\u5730\u56fe\u4e0a\u7ed8\u5236\u533a\u57df\uff0c\u901a\u8fc7\u6587\u672c\u63d0\u793a\u8bcd\uff08\u5982\u201c\u5efa\u7b51\u7269\u201d\uff09\u81ea\u52a8\u8bc6\u522b\u51fa\u76ee\u6807\u7269\u4f53\u3002'),
P([
  new TextRun({ text: '\u6838\u5fc3\u7279\u8272\uff1a', bold: true }),
]),
Bullet('\u652f\u6301\u6587\u672c\u63d0\u793a\u8bed\u4e49\u5206\u5272\uff08\u5efa\u7b51\u7269/\u8f66\u8f86/\u690d\u88ab/\u6c34\u4f53\u7b49 10 \u7c7b\uff09'),
Bullet('\u91c7\u7528\u74e6\u7247\u5206\u5272+\u653e\u5927\u7b56\u7565\uff0c\u5bf9\u5c0f\u76ee\u6807\u9ad8\u654f\u5ea6'),
Bullet('\u5b9e\u65f6\u8fdb\u5ea6\u53ef\u89c6\u5316 + GeoJSON/SHP \u5bfc\u51fa'),

// === 2 ===
H1('2. \u7cfb\u7edf\u67b6\u6784'),
H2('2.1 \u6574\u4f53\u67b6\u6784'),
P('\u524d\u540e\u7aef\u5206\u79bb\u67b6\u6784\uff0c\u901a\u8fc7 REST API \u901a\u4fe1\uff0c\u5171 3 \u4e2a\u6838\u5fc3\u6a21\u5757\uff1a'),

Num(new TextRun([{ text: '\u524d\u7aef (SAMPanel.jsx)', bold: true }), new TextRun(' \u2014 React+Leaflet\uff0c\u8d1f\u8d23\u5730\u56fe\u7ed8\u5236\u3001\u7528\u6237\u4ea4\u4e92\u3001\u8fdb\u5ea6\u663e\u793a\u3001\u7ed3\u679c\u53ef\u89c6\u5316')])),
Num(new TextRun([{ text: '\u540e\u7aef API (main.py)', bold: true }), new TextRun(' \u2014 FastAPI HTTP \u8def\u7531\u3001\u4efb\u52a1\u8c03\u5ea6\u3001\u8fdb\u5ea6\u67e5\u8be2\u63a5\u53e3(SSE)\u3001SHP \u4e0b\u8f7d')])),
Num(new TextRun([{ text: '\u63a8\u7406\u5f15\u64ce (sam_predict.py)', bold: true }), new TextRun(' \u2014 Python\u5b50\u8fdb\u7a0b\uff0c\u5305\u62ec\u5f71\u50cf\u88c1\u5206\u3001SAM\u63a8\u7406\u3001Mask\u8f6c\u591a\u8fb9\u5f62\u3001\u5750\u6807\u8f6c\u6362')]),

H2('2.2 \u6570\u636e\u6d41\u7a0b'),
new Table({ width: { size: 9000, type: WidthType.DXA }, columnWidths: [2200, 3200, 3600], rows: [
  Row(['\u73af\u8282', '\u6280\u672f', '\u8bf4\u660e'], [2200, 3200, 3600], true),
  Row(['\u7528\u6237\u2192\u540e\u7aef', 'POST /api/sam-detect', '\u53d1\u9001 GeoJSON+\u63d0\u793a\u8bcd\uff0c\u8fd4\u56de GeoJSON FeatureCollection']),
  Row(['\u7528\u6237\u2192\u540e\u7aef', 'GET /api/sam-progress/{id}', '\u8f6e\u8be2\u63a5\u8be2 SAM \u63a8\u7406\u5b9e\u65f6\u8fdb\u5ea6 (JSON \u6587\u4ef6)'],
  Row(['\u7528\u6237\u2192\u540e\u7aef', 'POST /api/sam-download', '\u5c06 GeoJSON \u6253\u5305\u4e3a SHP+ZIP \u4e0b\u8f7d')],
  Row(['\u540e\u7aef\u2192\u5b50\u7a0b', 'subprocess.run(sam_predict.py)', '\u901a\u8fc7\u73af\u5883\u53d8\u91cf SAM_PROGRESS_FILE \u4f20\u9012\u8fdb\u5ea6\u8def\u4ee5']),
]}),

// === 3 ===
H1('3. \u6838\u5fc3\u6280\u672f\uff1a\u74e6\u7247\u5206\u5272+\u653e\u5927\u63a8\u7406'),
H2('3.1 \u95ee\u9898\u80cc\u666f'),
P('\u76f4\u63a5\u5168\u56fe SAM \u63a8\u7406\u5b58\u5728\u7684\u95ee\u9898\uff1a\u5927\u56fe\u88ab\u7f29\u5c0f\u540e\u5c0f\u76ee\u6807\uff08\u5982\u519c\u6751\u6237\u3001\u5c45\u680b\u623f\uff09\u88ab\u706b\u7eaf\uff0c\u53ea\u80fd\u68c0\u5230\u7a00\u758f\u5b9e\u4f8b\u3002'),
P('\u89e3\u51b\u65b968\uff1a\u5c06\u5927\u56fe\u5206\u4e3a\u5c0f\u74e6\u7247\uff0c\u6bcf\u4e2a\u74e6\u7247\u653e\u5927 2~3 \u500d\u540e\u518d\u8fdb\u884c SAM \u63a8\u7406\uff0c\u4f7f\u5c0f\u76ee\u6807\u5728\u653e\u5927\u540e\u66f4\u6e05\u667e\uff0c\u4ece\u800c\u5b9e\u73b0\u7a20\u5bc6\u68c0\u6d4b\uff08Dense Detection\uff09\u3002'),

H2('3.2 \u74e6\u7247\u5206\u5272+\u653e\u5927\u7b56\u7565\u6d41\u7a0b'),
P('\u5b9e\u73b0\u4e3a run_tile_based_inference() \u51fd\u6570\uff0c\u6838\u5fc3\u6b65\u9aa4\uff1a'),

Num(new TextRun([{ text: '\u5f71\u50cf\u88c1\u5206', bold: true }), new TextRun(' \u2014 \u4fdd\u7559\u539f\u59cb\u5206\u8fa8\u7387\uff08target_size=None\uff09\uff0c\u4e0d\u505a\u7f29\u5c0f\uff0c\u4ee5\u786e\u4fdd\u5c0f\u76ee\u6807\u8be6\u6e90')]),
Num(new TextRun([{ text: '\u5207\u74e6\u7f51\u683c', bold: true }), new TextRun(' \u2014 tile_size=512px, stride=448(=512-64), overlap=64px \u91cd\u53e0')])),
Num(new TextRun([{ text: '\u74e6\u7247\u653e\u5927', bold: true }), new TextRun(' \u2014 \u6bcf\u4e2a\u74e6\u7247 LANCZOS \u653e\u5927 3 \u500d (zoom_factor=3)\uff0c\u4e0a\u9650\u2048x2048')])),
Num(new TextRun([{ text: 'SAM \u63a8\u7406', bold: true }), new TextRun(' \u2014 \u6bcf\u4e2a\u653e\u5927\u540e\u74e6\u7247\u72ec\u7acb8c8\u8fd0\u884c SegEarthOV3Segmentation.predict()')])),
Num(new TextRun([{ text: 'Mask \u7f29\u5c0f', bold: true }), new TextRun(' \u2014 cv2.resize() \u7f29\u56de\u539f\u59cb\u74e6\u7247\u5927\u5c0f\uff0c INTER_LINEAR \u63d2\u503c')])),
Num(new TextRun([{ text: '\u52a0\u6743\u878d\u5e76', bold: true }), new TextRun(' \u2014 \u91cd\u7ebf\u6e10\u6770\u5ea6(\u4e2d\u5fc3=1, \u8fb9\u7f18=\u6e10\u5ea6/(overlap+1)))\u52a0\u6743\u878d\u5e76\u878d\u5e76')]),
Num(new TextRun([{ text: '\u5f52\u4e00\u5316', bold: true }), new TextRun(' \u2014 accumulator/counter > 0.35 \u9608\u503c\u5316\u83b7\u5f97\u6700\u7ec8\u4e8\u503c mask')])),
Num(new TextRun([{ text: '\u591a\u8fb9\u5f62\u8f6c\u6362', bold: true }), new TextRun(' \u2014 cv2.findContours + approxPolyDP (\u7b80\u5ea6=0.005), \u6700\u5c0f\u9762\u79ef=50px')])),
Num(new TextRun([{ text: '\u5750\u6807\u8f6c\u6362', bold: true }), new TextRun(' \u2014 \u50cf\u7d20\u2192\u5730\u7406\u4eff\u5c04\u53d8\u6362 (EPSG:4326), Y\u8f74\u53cd\u5411')])),
Num(new TextRun([{ text: 'GeoJSON \u8f93\u51fa', bold: true }), new TextRun(' \u2014 Shapely Polygon \u9a8c\u9a8c + area \u8ba1\u7b97(m\xb2/\u4ea9)')])),
Num(new TextRun([{ text: 'SHP \u4fdd\u5b58', bold: true }), new TextRun(' \u2014 GeoDataFrame.to_file("sam_result.shp"), crs="EPSG:4326"')])),


// === 4 ===
H1('4. AI \u6a21\u578b\u8be6\u660e'),
H2('4.1 SegEarth-OV-3 (SAM3)'),
P('\u672c\u9879\u91c7\u7528\u7684\u5206\u5272\u6a21\u578b\u4e3a SegEarth-OV-3\uff0c\u57fa\u4e8e Meta SAM3 \u67b6\u6784\u3002\u5b83\u662f\u4e00\u4e2a\u652f\u6301\u6587\u672c\u63d0\u793a\u7684\u5168\u76ca\u8bed\u4e49\u5206\u5272\u6a21\u578b\uff0c\u53ef\u4ee5\u6839\u636e\u81ea\u7136\u8bed\u8a00\u63cf\u8ff0\u76f4\u63a5\u51fa\u76ee\u6807\u7c7b\u522b\u3002'),

new Table({ width: { size: 9000, type: WidthType.DXA }, columnWidths: [2500, 6500], rows: [
  Row(['\u6a21\u578b\u7ec4\u4ef6', '\u8be6\u7ec6\u8be4\u660e'], [2500, 6500], true),
  Row(['SAM3 \u67b6\u7840', '\u57fa\u4e8e Transformer \u7f16\u7801\u5668 + Vision Encoder, \u652f\u6301\u6587\u672c\u8f93\u5165(prompt)']],
  Row(['\u6743\u91cd', 'sam3.pt (3.4GB), \u5b58\u503c\u4e8e weights/sam3/\u76ee\u5f55']],
  Row(['\u8bcd\u5178', 'bpe_simple_vocab_16e6.txt.gz (BPE \u7f16\u7801\u8bcd\u5178, 16k \u8bcd\u6c42)]],
  Row(['\u7c7b\u522b\u6587\u8868', 'background/bareland/grass/road/car/tree/water/cropland/building(8)']],
  Row(['\u53cc\u5934\u878d\u5408', '\u8bed\u4e49\u5934(semantic) + \u5b9e\u4f8b\u5934(transformer decoder) \u878d\u878d\u673a\u5216']),
  Row(['\u6eda\u52a8\u7a97\u53e3', 'slide_stride=256, slide_crop=512, prob_thd=0.05~0.1']}),
]}),

H2('4.2 \u652f\u6301\u7684\u76EE\u6807\u7c7b\u522b'),
Code('name_list = ["background", "bareland,barren", "grass", "road", "car",\n             "tree,forest", "water,river", "cropland", "building,roof,house"]'),
P('\u5173\u952e\u8bcd\u5339\u914d\u7b56\u5f0f\uff1a\u7528\u6237\u8f93\u5165\u7684 prompt \u4e0e\u6bcf\u7c7b\u522b\u540d\u79f0\u8fdb\u884c\u5173\u952e\u8bcd\u5339\u914d\u3002\u4f8b\u5982\u201c\u5efa\u7b51\u7269\u201d\u5339\u914d\u5230 index=8 (building,roof,house)\u3002'),


// === 5 ===
H1('5. \u5b9e\u65f6\u8fdb\u5ea6\u8ffd\u8e2a'),
H2('5.1 \u8fdb\u5ea6\u67b6\u6784'),
P('\u901a\u8fc7\u6587\u4ef6\u7cfb\u7edf\u5b9e\u73b0\u5b9e\u65f6\u8fdb\u5ea6\u8ffd\u8e2a\uff0c\u524d\u7aef\u8f6e\u5f15\u8fdb\u5ea6 API\uff0c\u540e\u7aef\u8bfb\u5199\u8fdb\u5ea6\u6587\u4ef6\u5e76\u5bb9\u6570\u6362\uff1a'),

new Table({ width: { size: 9000, type: WidthType.DXA }, columnWidths: [2000, 7000], rows: [
  Row(['\u9636\u6bb5', '\u8fdb\u5ea6\u4fe1\u606f'], [2000, 7000], true),
  Row(['init', '\u89e3\u6790\u533a\u57df\u8fb9\u7548\uff0c\u51c\u5907\u88c1\u5206\u5f71\u50cf']),
  Row(['cropped', '\u5f71\u50cf\u88c1\u5206\u5b8c\u6210\uff0c 加\u8f7d SAM3 \u6a21\u578b']),
  Row(['inference_start', '\u5f00\u59cb SAM \u63a8\u7406 (0%~10%)']),
  Row(['inference', '\u74e6\u7247\u63a8\u7406\u4e2d... (10%~85%)\uff0c\u6bcf\u4e09\u4e2a\u74e6\u7247\u66f4\u65b\u4e00\u6b21']),
  Row(['inference_done', '\u63a8\u7406\u5b8c\u6210\uff0c Mask\u2192\u591a\u8fb9\u5f62 (85%~90%)']),
  Row(['done', '\u5b8c\u6210\uff01\u68c0\u5230 N \u4e2a\u76ee\u6807 (100%)']),
  Row(['error', '\u4efb\u4f55\u73af\u8bef\u5e94\uff0c\u5305\u542b\u9519\u7ec6\u4e1a\u4fe1\u606f']),
]}),

H2('5.2 \u524d\u7aef\u8fdb\u5ea6\u6761 UI'),
P('\u524d\u7aef\u91c7\u7528 setInterval \u6bcf 1.5s \u8f6e\u8be2 GET /api/sam-progress/{task_id}\uff0c\u663e\u793a\uff1a'),
Bullet('\u5f69\u8272\u8fdb\u5ea6\u6761\uff1a\u6839\u636e\u6bd4\u4f8b\uff0c 0%\u2192100%\u5206\u95f4'),
Bullet('\u6d41\u5149\u52a8\u753b\uff1a shimmer CSS animation\uff0c\u8fdb\u5ea6\u6761\u989c\u8272\u6e10\u6e10\u53d8\u5316'),
Bullet('\u6309\u94ae\u65e5\u5b57\uff1a\u5b9e\u65f6\u663e\u793a\u5f53\u524d\u9636\u6bb5\u6587\u606f\uff0c\u5982\u201c\u74e6\u7247\u63a8\u7406 3/6 (55%)\u201d'),


// === 6 ===
H1('6. \u90e8\u7f72\u4e0E\u90e8\u6587'),
new Table({ width: { size: 9000, type: WidthType.DXA }, columnWidths: [2500, 6500], rows: [
  Row(['\u6587\u4ef6', '\u8def\u4f4\u8def & \u529f\u80fd'], [2500, 6500], true),
  Row(['main.py', '\u540e\u7aef FastAPI \u4e3b\u6587\uff0c sam-detect/sam-progress/sam-download \u8def\u4e49']),
  Row(['sam_predict.py', '\u63a8\u7406\u811a\u672c\uff0c\u5305\u62ec crop/run_inference/mask_to_poly/pixel_to_geo/main']),
  Row(['SAMPanel.jsx', '\u524d\u7aef React \u7ec4\u4ef6\uff0c\u7ed8\u5236/\u63d0\u793a/\u8fdb\u5ea6\u6761/SHP\u4e0b\u8f7d']),
  Row(['segearthov3_segmentor.py', 'SAM3 \u5206\u5272\u6a21\u578b\u5c01\u5165\uff0csam3/model/ \u76ee\u5f55']),
  Row(['configs/my_name.txt', '9 \u7c7b\u76ee\u6807\u82f1\u540d\u6587\u4ef6\uff0c\u7528\u4e8e get_cls_idx() \u89e3\u6790']),
]}),


// === 7 ===
H1('7. \u90e8\u7f72\u4e0E\u53c2\u6570'),
P([
  new TextRun({ text: '\u5de5\u73af\u73af\u73af\uff1a', bold: true }),
]),
Bullet('\u524d\u7aef 3 \u5904\u786c\u7f16\u7801 URL \u2192 \u7edf\u4e00\u4e3a\u76f8\u5bf9\u8def\u5f84'),
Bullet('\u540e\u7aeb PM2 \u91cd\u542f \u2192 \u52a0\u8f7d SAM \u8def\u8def\u8def\u65b0\u4ee3\u7801'),
Bullet('Python conda env: sam \u2192 \u5b89\u88c5 huggingface_hub/transformers/einops/timm/ftfy/imageio \u7b49 8+ \u4f9d\u8d44'),
Bullet('os.chdir(SEG_DIR) \u2192 SAM3 \u6a21\u578b\u4f7\u7528\u76f8\u5bf9\u8def\u5f84\u52a0\u8f7d bpe \u8bcd\u5178'),
Bullet('_write_progress() \u2192 \u901a\u8fc7\u73af\u5883\u53d8\u91cf SAM_PROGRESS_FILE \u5199\u5199\u8fdb\u5ea6 JSON'),
Bullet('classname_path=None \u2192 get_cls_idx(None) \u5d4e\u81f4 open(None) \u62a5\u8bef\uff0c\u5df2\u6539\u6b63\u6b63\u4e3a\u914d\u7f6e\u8def\u4ef6\u8def\u5f84'),


new Paragraph({ children: [new TextRun('')], pageBreakBefore: true }),
H1('Appendix: \u547d\u4ee4\u53c2\u800'),
Code('# \u76f4\u63a8\u547d\u4ee4\ncurl -X POST http://localhost:8006/api/sam-detect -H "Content-Type: application/json" \\\n  -d \'{"geometry":{"type":"Polygon","coordinates":[[[115.66,32.22],[115.67,32.23],[115.68,32.23],[115.68,32.22],[115.66,32.22]]]},\\n        "prompt":"\u5efa\u7b51\u7269","mode":"rectangle"}\'\n\n# \u67e5\u8be2\u8fdb\u5ea6\ncurl http://localhost:8006/api/sam-progress/{task_id}\n\n# \u5b8c\u6574\u524d\u7aef\ncd /home/server/python/map_assistant_v1/frontend && npm run build && pm2 restart map-assistant-frontend'),
]}]);

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync('/home/server/python/map_assistant_v1/docs/SAM_Technical_Document.docx', buf);
  console.log('DOCX generated!');
});
