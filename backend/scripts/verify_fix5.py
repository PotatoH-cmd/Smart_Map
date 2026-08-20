"""fix5 诊断：高程点示意图不显示/缺失问题（童庙、淮堰-周集、道超、叶岗、沈营）。

对每个站点模拟 call() 的影像链路：
  1. 专属 tif 查找（精确/子目录/模糊）
  2. 单波段替换 / 裁剪失败回退（与 call() 相同顺序）
  3. _clip_drone_image + _draw_elev_figure
输出每个环节结果与高程图标注数，定位"不显示"根因。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DASHSCOPE_API_KEY', 'sk-e4990da94bfb4037be1f755fa586d048')

from tools.caisha_report_tool import CaishaReportTool, SITE_TIF_DIR, DEFAULT_TIF

SITES = ['史灌河童庙可采区', '灌河淮堰-周集可采区', '灌河道超可采区',
         '灌河叶岗可采区', '沈营可采区']


def find_site_tif(tool, site):
    """复刻 call() 前三步 tif 选择逻辑（精确→子目录→模糊→目录关键词）"""
    import re as _re
    site_tif = os.path.join(SITE_TIF_DIR, f'{site}.tif')
    if os.path.exists(site_tif):
        return site_tif, '精确匹配'
    for root, dirs, files in os.walk(SITE_TIF_DIR):
        for f in files:
            if f == f'{site}.tif':
                return os.path.join(root, f), '子目录精确匹配'
    core_kw = _re.sub(r'(可采区|采区|可采点|部分河段|砂场)', '', site).strip()
    kw_candidates = [site]
    if core_kw != site:
        kw_candidates.append(core_kw)
    for sep in ('、', '，', ',', '和'):
        parts = site.split(sep)
        if len(parts) >= 2:
            kw_candidates.extend(p.strip() for p in parts if len(p.strip()) >= 3)
    best_tif, best_score = '', 0
    for root, dirs, files in os.walk(SITE_TIF_DIR):
        for f in files:
            if not f.lower().endswith('.tif'):
                continue
            f_no_ext = _re.sub(r'\.tif$', '', f, flags=_re.IGNORECASE)
            for kw in kw_candidates:
                if kw in f_no_ext:
                    score = len(kw) / max(len(f_no_ext), 1) + (1 if kw == f_no_ext else 0)
                    if score > best_score:
                        best_score = score
                        best_tif = os.path.join(root, f)
                    break
    if best_tif and os.path.exists(best_tif):
        return best_tif, f'模糊匹配({os.path.basename(best_tif)})'
    return '', ''


def main():
    tool = CaishaReportTool()
    out_dir = '/home/server/python/map_assistant_v1/backend/static/reports'
    for site in SITES:
        print('=' * 70)
        print(f'站点: {site}')
        stats = tool._rtk_stats(site)
        if not stats:
            print('  无测点，跳过')
            continue
        print(f"  测点 {stats['count']} 个, 高程 {stats['zmin']:.2f}~{stats['zmax']:.2f}m")

        # 1. tif 选择（复刻 call 前三步）
        tif_path, src = find_site_tif(tool, site)
        if not tif_path:
            tif_path = DEFAULT_TIF
            src = '默认影像'
        print(f"  tif: {src} → {tif_path}")

        # 2. 单波段替换
        if tif_path and tif_path != DEFAULT_TIF:
            if tool._is_single_band(tif_path):
                hi = tool._find_high_res_tif(site)
                if hi:
                    print(f"  单波段 → 高分彩色影像: {hi}")
                    tif_path = hi
                else:
                    print('  单波段但未找到高分影像！')

        # 3. 裁剪
        ts = int(time.time())
        ortho_png = os.path.join(out_dir, f'fix5_{site[:2]}_{ts}_ortho.png')
        clip = tool._clip_drone_image(site, tif_path, 4547, ortho_png,
                                      points_4326=stats.get('points'))
        if clip is None and tif_path != DEFAULT_TIF:
            hi = tool._find_high_res_tif(site)
            if hi and hi != tif_path:
                clip = tool._clip_drone_image(site, hi, 4547, ortho_png,
                                              points_4326=stats.get('points'))
                if clip:
                    tif_path = hi
                    print('  专属裁剪失败 → 高分影像裁剪成功')
            if clip is None:
                clip = tool._clip_drone_image(site, DEFAULT_TIF, 4547, ortho_png,
                                              points_4326=stats.get('points'))
                if clip:
                    print('  专属/高分裁剪失败 → 默认影像裁剪成功')
        if clip:
            print(f"  裁剪成功: crs={clip['crs'].to_epsg() if clip['crs'] else '?'} "
                  f"extent={tuple(round(v,1) for v in clip['extent'])}")
        else:
            print('  裁剪失败（clip=None）')

        # 4. 高程图
        elev_png = os.path.join(out_dir, f'fix5_{site[:2]}_{ts}_elev.png')
        ok = tool._draw_elev_figure(site, stats['points'], clip, elev_png)
        print(f"  高程图: {'OK' if ok else 'FAIL'} → {elev_png}")

        # 5. 统计图片内容（红点覆盖范围）
        if ok:
            from PIL import Image
            import numpy as np
            im = Image.open(elev_png).convert('RGB')
            a = np.asarray(im).astype(int)
            r, g, b = a[..., 0], a[..., 1], a[..., 2]
            red = (r > 150) & (g < 100) & (b < 100)
            h, w = red.shape
            print(f"  图片 {im.size} 红色像素 {red.sum()}")
            if red.any():
                ys, xs = np.where(red)
                print(f"  红点分布 y:[{ys.min()},{ys.max()}] x:[{xs.min()},{xs.max()}]"
                      f" 宽占比{((xs.max()-xs.min())/w*100):.0f}% 高占比{((ys.max()-ys.min())/h*100):.0f}%")


if __name__ == '__main__':
    main()
