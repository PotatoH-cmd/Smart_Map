# -*- coding: utf-8 -*-
"""
batch_caisha_reports.merge_reports 合并编号结构测试（不依赖 DB / 网络）。

验证：
- 章节标题 1.批复采砂区年度实施情况监测与评估
- 县区章节 1.1平桥区采砂区监测与评估 / 1.2罗山县采砂区监测与评估
- 站点编号标题 1.1.1豫信平砂许〔2025〕第1号 / 1.2.1豫罗砂许〔2025〕第01号
- 小节编号 1.1.1.1 采砂场基本情况 / 1.1.1.2采砂场监测实施情况
- 每县末尾"采砂场监测评估意见"占位
- 图片保留、无原始标题残留
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.caisha_report_tool import BASIC_FIELDS, MISSING, CaishaReportTool  # noqa: E402
from scripts.batch_caisha_reports import merge_reports  # noqa: E402


@pytest.fixture(scope='module')
def tool():
    return CaishaReportTool()


def _make_site_docx(tool, tmp_path, site, site_title, display_name, section, monitor):
    from PIL import Image
    png = tmp_path / f'{display_name}.png'
    Image.new('RGB', (60, 60), 'blue').save(png)
    v = {
        'site_name': site,
        'site_title': site_title,
        'display_name': display_name,
        'permit_no': site_title,
        'plan_year': '2025',
        'basic_info': f'{display_name}采砂区域位于潢河某村河段。',
        'basic_section_name': section,
        'monitor_section_name': monitor,
        'drone_subject': f'{site_title}采砂区域',
        'drone_scope': '2025年度规划批复范围（桩号K11+900～K12+721）',
        'field_monitoring': f'2026年6月对{display_name}开展现场监测。',
        'drone_interpretation': '经解译，采区内无明显作业迹象。',
        'depth_evaluation': '实测平均高程25.019m，不低于控制开采高程下限22.19m。',
        'ctrl_bottom_elevation': '22.41～22.19m',
        'point_count': '100',
        'elev_min': '19.0',
        'elev_max': '30.0',
        'elev_avg': '25.0',
        'stake_range': 'K11+900～K12+721',
        'fig_ortho_no': '2',
        'fig_elev_no': '3',
        'generated_date': '2026年08月19日',
    }
    for k, _ in BASIC_FIELDS:
        v.setdefault(k, MISSING)
    result = tool._render_docx(v, str(png), str(png), [str(png)])
    assert result.get('success'), result
    return result['file_path']


def test_merge_numbering(tool, tmp_path):
    fp1 = _make_site_docx(tool, tmp_path, '淮河郑湾村可采区', '豫信平砂许〔2025〕第1号',
                          '淮河郑湾村可采区', '采砂场基本情况', '采砂场监测实施情况')
    fp2 = _make_site_docx(tool, tmp_path, '高店镇王湾村部分河段', '豫罗砂许〔2025〕第01号',
                          '高店镇王湾村部分河段', '采砂场基本情况', '采砂场监测实施情况')
    fp3 = _make_site_docx(tool, tmp_path, '浉淮村采区', '豫罗砂许〔2025〕第02号',
                          '浉淮村采区', '采砂场基本情况', '采砂场监测实施情况')
    entries = [
        (fp1, '淮河郑湾村可采区', '平桥区',
         {'site_title': '豫信平砂许〔2025〕第1号', 'basic_section_name': '采砂场基本情况',
          'monitor_section_name': '采砂场监测实施情况'}),
        (fp2, '高店镇王湾村部分河段', '罗山县',
         {'site_title': '豫罗砂许〔2025〕第01号', 'basic_section_name': '采砂场基本情况',
          'monitor_section_name': '采砂场监测实施情况'}),
        (fp3, '浉淮村采区', '罗山县',
         {'site_title': '豫罗砂许〔2025〕第02号', 'basic_section_name': '采砂场基本情况',
          'monitor_section_name': '采砂场监测实施情况'}),
    ]
    merged_path = merge_reports(entries, str(tmp_path), add_opinion=True)
    assert os.path.exists(merged_path)

    from docx import Document
    doc = Document(merged_path)
    full = '\n'.join(p.text for p in doc.paragraphs)

    assert '1.批复采砂区年度实施情况监测与评估' in full
    assert '1.1平桥区采砂区监测与评估' in full
    assert '1.2罗山县采砂区监测与评估' in full
    assert '1.1.1豫信平砂许〔2025〕第1号' in full
    assert '1.2.1豫罗砂许〔2025〕第01号' in full
    assert '1.2.2豫罗砂许〔2025〕第02号' in full
    assert '1.1.1.1 采砂场基本情况' in full
    assert '1.1.1.2采砂场监测实施情况' in full
    assert '1.2.2.2采砂场监测实施情况' in full
    assert '1.1.2平桥区采砂场监测评估意见' in full
    assert '1.2.3罗山县采砂场监测评估意见' in full
    # 原始站点标题不再残留（应已被改写为编号标题）
    assert '\n豫信平砂许〔2025〕第1号\n' not in full

    # 图片保留（每份报告 1 张现场图 + 正射 + 高程 = 3 张 → 共 9 张插图）
    img_count = len(doc.inline_shapes)
    assert img_count >= 9, f'插图数量异常: {img_count}'
    os.remove(merged_path)
    for fp in (fp1, fp2, fp3):
        if os.path.exists(fp):
            os.remove(fp)
