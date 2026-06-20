#!/usr/bin/env python3
"""
修复图表页：对有"图 X-X"引用但缺少 <img> 标签的页面，重新用 layout-parsing 处理。
优先字符少的页面（图表内容损失最严重）。

使用：
  python source-material/fix_diagram_pages.py --max-chars 1000 --parallel 4
"""
import json, os, re, subprocess, sys, io, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from config import API_URL, HEADERS, OUTPUT_DIR as PURE_DIR, IMG_DIR
    API_TOKEN = HEADERS["Authorization"].replace("bearer ", "")
    JOB_URL = API_URL
except ImportError:
    JOB_URL = "https://your-ocr-api.example.com/v2/ocr/jobs"
    PURE_DIR = Path("source-material/.ocr-output")
    IMG_DIR = Path("source-material/.ocr-work/images")
HEADERS = {"Authorization": f"bearer {TOKEN}"}


def find_diagram_pages(max_chars: int = 1000):
    """找出有图引用但缺 img 标签，且字符数 < max_chars 的页面"""
    targets = []
    for md_f in sorted(PURE_DIR.glob("page_*.md")):
        with open(md_f, encoding="utf-8") as f:
            raw = f.read()
        has_ref = bool(re.search(r"图\s*\d+[\.\-]\d+", raw))
        has_img = "<img " in raw
        if has_ref and not has_img:
            text = re.sub(r"<[^>]+>", "", raw)
            chars = len(re.sub(r"\s+", "", text))
            if chars < max_chars:
                pg = int(md_f.stem.split("_")[1])
                refs = re.findall(r"图\s*\d+[\.\-]\d+", raw)
                targets.append((pg, chars, list(set(refs))[:3]))
    targets.sort(key=lambda x: x[1])
    return targets


def process_page(pg: int) -> bool:
    """用 layout-parsing 重新处理单页，只在新结果更好时保存"""
    label = f"p{pg:04d}"
    img_path = IMG_DIR / f"page_{pg:04d}.png"
    if not img_path.exists():
        print(f"  [{label}] 图片缺失，跳过")
        return False

    # 读取旧结果
    old_path = PURE_DIR / f"page_{pg:04d}.md"
    with open(old_path, encoding="utf-8") as f:
        old_raw = f.read()
    old_text = re.sub(r"<[^>]+>", "", old_raw)
    old_chars = len(re.sub(r"\s+", "", old_text))
    old_has_img = "<img " in old_raw

    print(f"  [{label}] {old_chars}c, {img_path.stat().st_size/1024:.0f}KB...", end=" ", flush=True)

    # 提交 layout-parsing job
    try:
        r = subprocess.run([
            "curl", "-sSL", "-H", f"Authorization: bearer {TOKEN}",
            "-F", "model=PaddleOCR-VL-1.6",
            "-F", 'optionalPayload={"useLayoutDetection":true,"useDocOrientationClassify":false,"useDocUnwarping":false,"useChartRecognition":false}',
            "-F", f"file=@{img_path}", JOB_URL,
        ], capture_output=True, timeout=120)

        if r.returncode != 0:
            print("上传失败")
            return False
        job_id = json.loads(r.stdout.decode("utf-8", errors="replace"))["data"]["jobId"]
    except Exception as e:
        print(f"上传异常: {e}")
        return False

    print(f"job={job_id[-12:]} polling...", end=" ", flush=True)

    # 轮询
    deadline = time.time() + 300
    data = None
    while time.time() < deadline:
        r = subprocess.run(
            ["curl", "-sSL", "-H", f"Authorization: bearer {TOKEN}",
             f"{JOB_URL}/{job_id}"], capture_output=True, timeout=30)
        if r.returncode != 0:
            time.sleep(3)
            continue
        try:
            d = json.loads(r.stdout.decode("utf-8", errors="replace"))["data"]
        except Exception:
            time.sleep(3)
            continue
        if d["state"] == "done":
            data = d
            break
        elif d["state"] == "failed":
            print(f"失败: {d.get('errorMsg', '?')}")
            return False
        time.sleep(3)

    if not data:
        print("超时")
        return False

    # 下载结果
    try:
        json_url = data["resultUrl"]["jsonUrl"]
    except KeyError:
        print("无结果URL")
        return False

    r = subprocess.run(["curl", "-sSL", json_url], capture_output=True, timeout=60)
    if r.returncode != 0:
        print("下载失败")
        return False

    try:
        raw = r.stdout.decode("utf-8", errors="replace")
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        result = json.loads(lines[0])["result"]
    except Exception:
        print("解析失败")
        return False

    # 提取 markdown + 下载图片
    md_parts = []
    total_imgs = 0
    for res in result.get("layoutParsingResults", []):
        md_parts.append(res["markdown"]["text"])
        for fname, url in res["markdown"].get("images", {}).items():
            basename = fname.replace("imgs/", "").split("/")[-1]
            outpath = PURE_DIR / "imgs" / basename
            (PURE_DIR / "imgs").mkdir(parents=True, exist_ok=True)
            if not outpath.exists() or outpath.stat().st_size == 0:
                subprocess.run(["curl", "-sSL", "-o", str(outpath), url],
                               capture_output=True, timeout=30)
                total_imgs += 1

    new_md = "\n\n".join(md_parts)
    new_text = re.sub(r"<[^>]+>", "", new_md)
    new_chars = len(re.sub(r"\s+", "", new_text))
    new_has_img = "<img " in new_md

    # 决定是否保存（新结果有图片 或 字符数改善>10%）
    improved = new_chars > old_chars * 1.1
    gained_img = new_has_img and not old_has_img

    if gained_img or improved:
        with open(old_path, "w", encoding="utf-8") as f:
            f.write(new_md)
        img_tag = "+img" if gained_img else ""
        char_tag = f"+{new_chars-old_chars}c" if improved else ""
        print(f"OK {old_chars}c→{new_chars}c {img_tag} {char_tag}")
        return True
    else:
        print(f"SKIP {old_chars}c→{new_chars}c (无改善)")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-chars", type=int, default=1000, help="字符数阈值（低于此值的页面才修复）")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="限制数量")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    targets = find_diagram_pages(args.max_chars)
    print(f"待修复（<{args.max_chars}c 的有图缺图页面）: {len(targets)} 页\n")

    if not targets:
        print("[OK] 无需修复")
        return

    # 显示前20
    print("页码  | 字符数 | 图编号")
    print("------|--------|--------")
    for pg, chars, refs in targets[:20]:
        print(f"p{pg:04d} | {chars:6d} | {', '.join(refs)}")
    if len(targets) > 20:
        print(f"... 共 {len(targets)} 页")

    if args.limit > 0:
        targets = targets[:args.limit]

    print(f"\n开始修复 {len(targets)} 页...\n")

    t0 = time.time()
    ok = 0
    pages = [pg for pg, _, _ in targets]

    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(process_page, pg): pg for pg in pages}
            for fut in as_completed(futures):
                if fut.result():
                    ok += 1
    else:
        for i, pg in enumerate(pages):
            print(f"[{i+1}/{len(pages)}]", end="")
            if process_page(pg):
                ok += 1

    elapsed = time.time() - t0
    print(f"\n[DONE] 修复 {ok}/{len(pages)} 页, 耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
