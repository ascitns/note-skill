#!/usr/bin/env python3
"""
逐页 OCR 上传引擎。

将 PNG 图片逐页上传到 OCR API，使用混合策略：
  1. 先 pure OCR 模式识别全页文字
  2. 结果 <200 字符 → 自动 fallback 到 layout 模式

使用：
  python ocr_upload_images.py                    # 全量处理
  python ocr_upload_images.py --start 100         # 从第 100 页开始
  python ocr_upload_images.py --start 100 --count 50  # 增量 50 页
  python ocr_upload_images.py --parallel 4        # 4 并发
  python ocr_upload_images.py --test              # 测试 1 页

依赖：subprocess + curl（无需 requests 模块）
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

# 导入共享配置
try:
    from config import (
        API_URL, HEADERS, MODEL,
        IMG_DIR, OUTPUT_DIR, PROGRESS_FILE, IMGS_DIR,
        PURE_OCR_PAYLOAD, LAYOUT_PAYLOAD, MIN_CHAR_THRESHOLD,
        ensure_dirs, valid_page,
    )
except ImportError:
    # 回退到硬编码默认值（允许独立运行）
    API_URL = "https://your-ocr-api.example.com/v2/ocr/jobs"
    HEADERS = {"Authorization": "bearer your-token-here"}
    MODEL = "PaddleOCR-VL-1.6"
    IMG_DIR = Path("source-material/.ocr-work/images")
    OUTPUT_DIR = Path("source-material/.ocr-output")
    PROGRESS_FILE = OUTPUT_DIR / "progress.json"
    IMGS_DIR = OUTPUT_DIR / "imgs"
    PURE_OCR_PAYLOAD = {"useLayoutDetection": False, "promptLabel": "ocr"}
    LAYOUT_PAYLOAD = {"useLayoutDetection": True}
    MIN_CHAR_THRESHOLD = 200

    def ensure_dirs():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        IMGS_DIR.mkdir(parents=True, exist_ok=True)

    def valid_page(n):
        f = OUTPUT_DIR / f"page_{n:04d}.md"
        return f.exists() and f.stat().st_size > 50


# ── 进度管理 ──────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"submitted": {}, "done": [], "failed": []}


def save_progress(p):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, indent=2)


# ── API 操作 ──────────────────────────────────────────────

def submit_image(img_path: Path, use_layout: bool = False) -> str | None:
    """上传单张图片，返回 jobId"""
    payload = LAYOUT_PAYLOAD if use_layout else PURE_OCR_PAYLOAD
    payload_str = json.dumps(payload)

    try:
        r = subprocess.run([
            "curl", "-sSL",
            "-H", f"Authorization: {HEADERS['Authorization']}",
            "-F", f"model={MODEL}",
            "-F", f"optionalPayload={payload_str}",
            "-F", f"file=@{img_path}",
            API_URL,
        ], capture_output=True, timeout=120, text=True)

        if r.returncode == 0:
            data = json.loads(r.stdout)["data"]
            return data.get("jobId")
    except Exception as e:
        pass
    return None


def poll_job(job_id: str, interval: int = 3, max_wait: int = 300) -> dict | None:
    """轮询直到完成或超时，返回 data dict 或 None"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = subprocess.run([
                "curl", "-sSL",
                "-H", f"Authorization: {HEADERS['Authorization']}",
                f"{API_URL}/{job_id}",
            ], capture_output=True, timeout=30, text=True)
            if r.returncode != 0:
                time.sleep(interval)
                continue

            data = json.loads(r.stdout)["data"]
            state = data.get("state", "unknown")

            if state == "done":
                return data
            elif state == "failed":
                return None
            time.sleep(interval)
        except Exception:
            time.sleep(interval)
    return None


def download_result(data: dict) -> str | None:
    """下载 OCR 结果——Markdown 文本 + 关联图片"""
    try:
        json_url = data["resultUrl"]["jsonUrl"]
    except KeyError:
        return None

    r = subprocess.run(["curl", "-sSL", json_url],
                       capture_output=True, timeout=60, text=True)
    if r.returncode != 0:
        return None

    try:
        lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
        result = json.loads(lines[0])["result"]
    except (json.JSONDecodeError, KeyError, IndexError):
        return None

    md_parts = []
    for res in result.get("layoutParsingResults", []):
        md_text = res["markdown"]["text"]
        md_parts.append(md_text)

        # 下载图片
        imgs = res["markdown"].get("images", {})
        for fname, url in imgs.items():
            basename = fname.replace("imgs/", "").split("/")[-1]
            outpath = IMGS_DIR / basename
            if outpath.exists() and outpath.stat().st_size > 0:
                continue
            subprocess.run(
                ["curl", "-sSL", "-o", str(outpath), url],
                capture_output=True, timeout=30,
            )

    return "\n\n".join(md_parts)


# ── 单页处理 ──────────────────────────────────────────────

def process_page(page_num: int, progress: dict) -> bool:
    """处理单页：pure OCR → fallback layout 如果结果太短"""
    img_path = IMG_DIR / f"page_{page_num:04d}.png"
    if not img_path.exists():
        return False

    label = f"p{page_num:04d}"

    for attempt, (is_layout, strategy_tag) in enumerate([
        (False, "P"), (True, "L")
    ]):
        tag = f"[{strategy_tag}]" if attempt > 0 else ""
        print(f"  [{label}]{tag} uploading ({img_path.stat().st_size/1024:.0f}KB)...",
              end=" ", flush=True)

        t0 = time.time()
        job_id = submit_image(img_path, use_layout=is_layout)
        if not job_id:
            print("upload failed")
            if attempt == 0:
                continue
            return False

        print(f"{time.time()-t0:.0f}s {job_id[-12:]}", end="", flush=True)
        progress["submitted"][str(page_num)] = job_id
        save_progress(progress)

        data = poll_job(job_id)
        if not data:
            if attempt == 0:
                print(" poll failed, trying layout...")
                continue
            return False

        md_text = download_result(data)
        if not md_text:
            if attempt == 0:
                print(" empty, trying layout...")
                continue
            return False

        # 检查结果是否太短
        text_only = re.sub(r"<[^>]+>", "", md_text).strip()
        text_only = re.sub(r"\s+", " ", text_only)

        if attempt == 0 and len(text_only) < MIN_CHAR_THRESHOLD:
            print(f" {len(text_only)}c too short, trying layout...")
            continue

        md_path = OUTPUT_DIR / f"page_{page_num:04d}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        ok_tag = "[OK]" if attempt == 0 else "[OK-L]"
        print(f" {ok_tag} {len(text_only)}c")
        progress.setdefault("done", []).append(page_num)
        save_progress(progress)
        return True

    print(" all strategies failed")
    return False


# ── 主流程 ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="逐页 OCR 上传引擎")
    parser.add_argument("--test", action="store_true", help="测试 1 张")
    parser.add_argument("--start", type=int, default=0, help="起始页")
    parser.add_argument("--count", type=int, default=0, help="处理页数（0=全部）")
    parser.add_argument("--parallel", type=int, default=1, help="并发数")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)
    ensure_dirs()
    progress = load_progress()

    # 确定待处理页
    img_files = sorted(IMG_DIR.glob("page_*.png"))
    all_pages = sorted(int(f.stem.split("_")[1]) for f in img_files)

    if args.test:
        todo = [all_pages[0]]
        print(f"[TEST] 测试页 {todo[0]}\n")
    else:
        todo = []
        for p in all_pages:
            if p < args.start:
                continue
            if p in progress.get("done", []):
                continue
            if valid_page(p):
                progress.setdefault("done", []).append(p)
                continue
            todo.append(p)
        if args.count > 0:
            todo = todo[:args.count]

    total = len(todo)
    done = len(progress.get("done", []))
    print(f"[OCR] 总计 {len(all_pages)} 页, 已完成 {done}, 待处理 {total}")
    if args.parallel > 1:
        print(f"  并发: {args.parallel}")

    save_progress(progress)

    if not todo:
        print("[OK] 全部完成!")
        return

    t0 = time.time()
    ok = 0

    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(process_page, p, progress): p for p in todo}
            for fut in as_completed(futures):
                if fut.result():
                    ok += 1
    else:
        for i, page in enumerate(todo):
            print(f"\n[{i+1}/{total}]", end="")
            if process_page(page, progress):
                ok += 1

    elapsed = time.time() - t0
    print(f"\n[DONE] {ok}/{total} 成功, 耗时 {elapsed/60:.1f} 分钟")


if __name__ == "__main__":
    main()
