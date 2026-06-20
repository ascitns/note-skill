#!/usr/bin/env python3
"""
检测 OCR 输出中被图表标签污染的页面。
模式：连续出现重复的短行（如图中标注被当作独立文本行提取），
导致实际散文内容被淹没。

检测维度：
  1. 重复短行密度 —— 连续区域内相同短行出现次数
  2. 语义密度 —— 排除 HTML 标签和空白后的有效文本占比
  3. 行重复率 —— 整体页面中重复行的比例

使用：
  python detect_garbled_pages.py              # 扫描全部页面
  python detect_garbled_pages.py --threshold 0.3  # 自定义阈值
  python detect_garbled_pages.py --fix        # 标记问题页面，用 layout 重处理
"""
import re, sys, io, os, argparse
from collections import Counter
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from config import OUTPUT_DIR as PURE_DIR
except ImportError:
    PURE_DIR = Path("source-material/.ocr-output")

def analyze_page(pg: int) -> dict | None:
    """分析单页，返回垃圾指标"""
    f = PURE_DIR / f"page_{pg:04d}.md"
    if not f.exists() or f.stat().st_size < 50:
        return None

    with open(f, encoding="utf-8") as fh:
        raw = fh.read()

    # 拆分行为纯文本（去 HTML 标签）
    text_lines = []
    for line in raw.split("\n"):
        stripped = re.sub(r"<[^>]+>", "", line).strip()
        if stripped:
            text_lines.append(stripped)

    if not text_lines:
        return None

    total_lines = len(text_lines)

    # 短行：长度 <= 15 字符的非空白行
    short_lines = [l for l in text_lines if len(l) <= 15]

    # 重复短行计数：同一短行出现 >= 3 次
    short_counter = Counter(short_lines)
    repeated_short = {k: v for k, v in short_counter.items() if v >= 3}
    repeated_short_count = sum(v for v in repeated_short.values())

    # 指标 1：重复短行占总行数的比例
    repeat_ratio = repeated_short_count / total_lines if total_lines else 0

    # 指标 2：唯一的重复短行数量（越多说明标签越碎）
    unique_repeats = len(repeated_short)

    # 指标 3：连续重复块的最大长度（最直接的标签污染信号）
    max_consecutive_repeat = 0
    current_run = 0
    for line in text_lines:
        if len(line) <= 15 and short_counter.get(line, 0) >= 3:
            current_run += 1
            max_consecutive_repeat = max(max_consecutive_repeat, current_run)
        else:
            current_run = 0

    # 综合评分
    score = 0
    if repeat_ratio > 0.3:
        score += 1
    if unique_repeats >= 5:
        score += 1
    if max_consecutive_repeat >= 8:
        score += 2  # 连续重复是最强的信号
    if repeat_ratio > 0.5:
        score += 2

    return {
        "pg": pg,
        "total_lines": total_lines,
        "repeat_ratio": round(repeat_ratio, 3),
        "unique_repeats": unique_repeats,
        "max_consecutive": max_consecutive_repeat,
        "score": score,
        "sample": [k for k, v in repeated_short.items()][:5] if repeated_short else [],
    }


def main():
    parser = argparse.ArgumentParser(description="检测 OCR 图表标签污染页面")
    parser.add_argument("--threshold", type=int, default=3, help="评分阈值（>=此值标记为问题页）")
    parser.add_argument("--limit", type=int, default=0, help="只显示前 N 个")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    results = []
    for pg in range(0, 775):
        r = analyze_page(pg)
        if r:
            results.append(r)

    # 按评分降序
    results.sort(key=lambda r: (-r["score"], -r["repeat_ratio"]))

    # 统计
    by_score = {0: [], 1: [], 2: [], 3: [], 4: [], 5: []}
    for r in results:
        by_score.setdefault(r["score"], []).append(r)

    print(f"扫描 {len(results)} 页\n")
    print(f"评分分布:")
    for s in sorted(by_score.keys(), reverse=True):
        cnt = len(by_score[s])
        if cnt > 0:
            bar = "█" * min(cnt // 5, 40)
            print(f"  >= {s}: {cnt:4d} 页 {bar}")

    # 列出问题页面
    problem = [r for r in results if r["score"] >= args.threshold]
    if args.limit > 0:
        problem = problem[:args.limit]

    if problem:
        print(f"\n问题页面（评分 >= {args.threshold}）: {len(problem)} 页\n")
        print(f"{'页码':>6} | {'评分':>4} | {'重复率':>6} | {'连续重复':>6} | 重复标签样例")
        print("-" * 80)
        for r in problem:
            samples = ", ".join(r["sample"][:3])
            print(f"p{r['pg']:04d} | {r['score']:>4} | {r['repeat_ratio']:>6.1%} | "
                  f"{r['max_consecutive']:>6} | {samples}")

        if not args.limit or len([r for r in results if r["score"] >= args.threshold]) > args.limit:
            print(f"\n... 共 {len([r for r in results if r['score'] >= args.threshold])} 页")
        print(f"\n修复: python source-material/ocr_upload_images.py 逐页重处理，使用 layout 模式")
    else:
        print(f"\n[OK] 无问题页面")


if __name__ == "__main__":
    main()
