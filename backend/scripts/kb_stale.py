#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过期切片扫描：列出源文件已改、但库内仍是旧版的文档。

用法：
  python scripts/kb_stale.py           # 只列出过期文档
  python scripts/kb_stale.py --json    # 以 JSON 输出（供外部调用）
  python scripts/kb_stale.py --fix     # 对过期文档逐个执行增量更新
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# doc_id 前缀 → 对应的增量更新脚本（用于 --fix 自动分派）
FIXERS = {
    "huangchuan_2025_shishifangan": "ingest_caisha_md.py",
    "2026年6月采砂现场监测": "ingest_jiance_md.py",
    "shangcheng_2025_shishifangan": "ingest_shishifangan_multi.py",
    "pingqiao_2025_shishifangan": "ingest_shishifangan_multi.py",
    "luoshan_2025_shishifangan": "ingest_shishifangan_multi.py",
    "gushi_2025_shishifangan": "ingest_shishifangan_multi.py",
}


def main():
    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BACKEND_DIR, ".env"))

    from kb_pipeline import Manifest, scan_stale

    manifest = Manifest()
    stale = scan_stale(manifest)

    if "--json" in sys.argv:
        print(json.dumps(stale, ensure_ascii=False, indent=2))
    else:
        c = manifest.counts()
        print("清单：%d 个文档 / %d 个切片" % (c["documents"], c["chunks"]))
        if not stale:
            print("\n全部文档与源文件同步，无过期切片。")
        else:
            print("\n发现 %d 个文档的切片已过期：" % len(stale))
            for s in stale:
                print("  - %s" % s["doc_id"])
                print("      %s" % s["name"])
                print("      源文件: %s" % s["file"])
                print("      库内 mtime=%.0f  磁盘 mtime=%.0f" % (s["db_mtime"] or 0, s["fs_mtime"]))

    if "--fix" in sys.argv and stale:
        print("\n开始增量更新 ...")
        import subprocess

        seen = set()
        for s in stale:
            script = FIXERS.get(s["doc_id"])
            if not script:
                print("  跳过 %s（未登记对应的入库脚本，请手动处理）" % s["doc_id"])
                continue
            if script in seen:
                continue
            seen.add(script)
            cmd = [sys.executable, os.path.join(os.path.dirname(__file__), script)]
            if script == "ingest_shishifangan_multi.py":
                slug = s["doc_id"].split("_")[0]
                cmd += ["--doc", slug]
            print("  执行 %s" % " ".join(cmd))
            subprocess.run(cmd, check=False)

        left = scan_stale(Manifest())
        print("\n修复后剩余过期文档: %d" % len(left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
