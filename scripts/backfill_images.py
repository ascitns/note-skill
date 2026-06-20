#!/usr/bin/env python3
"""
回填缺失的图片：从 PaddleOCR API 重新获取已处理页面的图片。

使用：
  python backfill_images.py              # 回填所有已完成页面
  python backfill_images.py --parallel 4 # 4 并发回填

背景：ocr_upload_images.py 的 download_result() 只保存了 markdown 文本，
遗漏了 markdown.images 中引用的图片。此脚本利用 progress.json 中存储的
jobId 重新获取结果，下载缺失的图片。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 共享配置
try:
    from config import API_URL, HEADERS, OUTPUT_DIR, PROGRESS_FILE, IMGS_DIR, ensure_dirs
except ImportError:
    API_URL = "https://your-ocr-api.example.com/v2/ocr/jobs"
    HEADERS = {"Authorization": "bearer your-token-here"}
    OUTPUT_DIR = Path("source-material/.ocr-output")
    PROGRESS_FILE = OUTPUT_DIR / "progress.json"
    IMGS_DIR = OUTPUT_DIR / "imgs"
    def ensure_dirs():
        IMGS_DIR.mkdir(parents=True, exist_ok=True)


def get_json_url(job_id: str) -> str | None:
    """从 jobId 获取 JSON 结果 URL"""
    import subprocess

    r = subprocess.run(
        ["curl", "-sSL", "-H", f"Authorization: bearer {TOKEN}",
         f"{JOB_URL}/{job_id}"],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.decode("utf-8", errors="replace"))["data"]
        return data["resultUrl"]["jsonUrl"]
    except (KeyError, json.JSONDecodeError):
        return None


def download_images_for_page(page_num: int, job_id: str) -> int:
    """下载单页的所有图片，返回下载数量"""
    import subprocess

    label = f"p{page_num:04d}"

    # 1. 获取 JSON URL
    json_url = get_json_url(job_id)
    if not json_url:
        print(f"  [{label}] 获取 JSON URL 失败")
        return 0

    # 2. 获取 JSON 结果
    r = subprocess.run(["curl", "-sSL", json_url], capture_output=True, timeout=60)
    if r.returncode != 0:
        print(f"  [{label}] 下载 JSON 失败")
        return 0

    try:
        raw = r.stdout.decode("utf-8", errors="replace")
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        if not lines:
            return 0
        result = json.loads(lines[0])["result"]
    except (json.JSONDecodeError, KeyError, IndexError):
        print(f"  [{label}] JSON 解析失败")
        return 0

    # 3. 提取所有图片
    downloaded = 0
    for lr in result.get("layoutParsingResults", []):
        imgs = lr.get("markdown", {}).get("images", {})
        for fname, url in imgs.items():
            # 文件名可能包含 "imgs/" 前缀，去除
            basename = fname.replace("imgs/", "").split("/")[-1]
            outpath = IMGS_DIR / basename

            if outpath.exists() and outpath.stat().st_size > 0:
                downloaded += 1
                continue

            r2 = subprocess.run(
                ["curl", "-sSL", "-o", str(outpath), url],
                capture_output=True, timeout=30,
            )
            if r2.returncode == 0 and outpath.stat().st_size > 0:
                downloaded += 1

    if downloaded > 0:
        print(f"  [{label}] {downloaded} 张图片已下载")
    return downloaded


def main():
    parser = argparse.ArgumentParser(description="回填 PaddleOCR 缺失图片")
    parser.add_argument("--parallel", type=int, default=1, help="并发数")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量（测试用）")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)
    IMGS_DIR.mkdir(parents=True, exist_ok=True)

    # 加载进度
    if not PROGRESS_FILE.exists():
        print("[ERR] progress.json 不存在")
        return

    with open(PROGRESS_FILE, encoding="utf-8") as f:
        progress = json.load(f)

    submitted = progress.get("submitted", {})
    done = set(progress.get("done", []))
    print(f"已完成页面: {len(done)}, 有 jobId: {len(submitted)}")

    # 筛选需要回填的页面（只处理有图片引用的页面）
    # 先扫描哪些页面有 <img 标签
    pages_with_imgs = set()
    for md_file in sorted(OUTPUT_DIR.glob("page_*.md")):
        try:
            with open(md_file, encoding="utf-8") as f:
                if "<img " in f.read():
                    p = int(md_file.stem.split("_")[1])
                    pages_with_imgs.add(p)
        except Exception:
            pass

    print(f"包含图片引用的页面: {len(pages_with_imgs)}")

    # 构建任务列表
    tasks = []
    for page_str, job_id in submitted.items():
        page_num = int(page_str)
        if page_num in pages_with_imgs:
            tasks.append((page_num, job_id))

    if args.limit > 0:
        tasks = tasks[:args.limit]

    # 检查哪些图片已经存在
    existing_imgs = set()
    if IMGS_DIR.exists():
        existing_imgs = {f.name for f in IMGS_DIR.iterdir()}
    print(f"已有图片: {len(existing_imgs)}")

    # 还缺多少
    needed_total = 0
    for page_num, _ in tasks:
        md_path = OUTPUT_DIR / f"page_{page_num:04d}.md"
        if md_path.exists():
            import re
            with open(md_path, encoding="utf-8") as f:
                refs = re.findall(r'src="imgs/([^"]+)"', f.read())
            missing = [r for r in refs if r not in existing_imgs]
            needed_total += len(missing)
    print(f"缺失图片: {needed_total}")
    if needed_total == 0:
        print("[OK] 所有图片已就位!")
        return

    print(f"\n开始回填 {len(tasks)} 个页面...\n")

    t0 = time.time()
    total_imgs = 0
    ok_pages = 0

    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(download_images_for_page, p, j): p
                for p, j in tasks
            }
            for fut in as_completed(futures):
                try:
                    n = fut.result()
                    total_imgs += n
                    if n > 0:
                        ok_pages += 1
                except Exception as e:
                    print(f"  错误: {e}")
    else:
        for i, (page_num, job_id) in enumerate(tasks):
            print(f"[{i+1}/{len(tasks)}]", end="")
            n = download_images_for_page(page_num, job_id)
            total_imgs += n
            if n > 0:
                ok_pages += 1

    elapsed = time.time() - t0
    print(f"\n[DONE] {ok_pages}/{len(tasks)} 页, {total_imgs} 张图片, 耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
