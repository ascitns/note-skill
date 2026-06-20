#!/usr/bin/env python3
"""
OCR 文字疏漏检测与修复 — 多维度检测页面的文字缺失，支持重新处理。

检测维度：
  1. 字符密度异常 — 与相邻页差距 >50% 标记
  2. RapidOCR 覆盖率 — <75% 标记为可疑
  3. 结构元素缺失 — 缺失"图 X-X"编号或章节标题
  4. 极短页面 — <200 有效字符

修复策略：
  1. 对疑似疏漏页，重新用 layout-parsing 模式提交 PaddleOCR API
  2. 生成 QA 报告

使用：
  python detect_omissions.py                    # 检测 + 生成报告
  python detect_omissions.py --fix              # 检测 + 修复低质量页面
  python detect_omissions.py --fix --parallel 4 # 4 并发修复
  python detect_omissions.py --report-only      # 仅基于已有数据生成报告
"""
import argparse
import json
import os
import re
import sys
import io
import time
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# --- 配置 ---
try:
    from config import OUTPUT_DIR as PURE_DIR
except ImportError:
    PURE_DIR = Path("source-material/.ocr-output")
OCR_DIR = Path("source-material/.ocr-work/ocr_results")
IMG_DIR = Path("source-material/.ocr-work/images")
OUTPUT_DIR = PURE_DIR
REPORT_FILE = PURE_DIR / "qa_report.md"

TOKEN = "46a3acc84f8b8d28c00cfefc065a12e292e64ec4"
JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
HEADERS = {"Authorization": f"bearer {TOKEN}"}
MODEL = "PaddleOCR-VL-1.6"

# RapidOCR 只处理了部分页面（~91页），设置标志
RAPID_OCR_LIMIT = 91


@dataclass
class PageQuality:
    """单页质量评估"""
    pg: int
    pure_chars: int = 0
    ocr_chars: int = 0
    rapid_coverage: float = 1.0
    has_table: bool = False
    has_formula: bool = False
    has_code: bool = False
    has_diagram_ref: bool = False
    has_section_header: bool = False
    text_preview: str = ""

    # 评分
    density_anomaly: bool = False
    rapid_low: bool = False
    is_very_short: bool = False
    structure_missing: bool = False

    score: int = 0  # 0=OK, 1=WARN, 2=BAD, 3=CRITICAL


def analyze_page(pg: int) -> Optional[PageQuality]:
    """分析单页质量，返回 PageQuality 或 None（页面不存在）"""
    pure_f = PURE_DIR / f"page_{pg:04d}.md"
    if not pure_f.exists():
        return None

    with open(pure_f, encoding="utf-8") as f:
        raw = f.read()

    # 去除 HTML 标签后的纯文本
    text_only = re.sub(r"<[^>]+>", "", raw).strip()
    text_only = re.sub(r"\n\s*\n", "\n", text_only)
    # 去除空白用于计数字符
    compact = re.sub(r"\s+", "", text_only)
    pure_chars = len(compact)

    q = PageQuality(pg=pg, pure_chars=pure_chars)

    # 内容类型检测
    q.has_table = "<table>" in raw or "|" in text_only and text_only.count("|") > 3
    q.has_formula = "$" in raw
    q.has_code = bool(re.search(r"\b(mov|add|sub|push|pop|call|ret|jmp|lea|cmp|test|xor)\b", raw, re.I))
    q.has_diagram_ref = bool(re.search(r"图\s*\d+[\.\-]\d+", text_only))
    q.has_section_header = bool(re.search(r"^#{1,4}\s", text_only, re.M)) or \
        bool(re.search(r"^\d+\.\d+[\.\d]*\s+\S", text_only, re.M))
    q.text_preview = text_only[:150]

    # === 维度 2: RapidOCR 覆盖率 ===
    ocr_f = OCR_DIR / f"page_{pg:04d}.json"
    if ocr_f.exists() and pg < RAPID_OCR_LIMIT:
        try:
            with open(ocr_f, encoding="utf-8") as f:
                ocr_data = json.load(f)
            ocr_text = "".join(l["text"] for l in ocr_data.get("lines", []))
            q.ocr_chars = len(re.sub(r"\s+", "", ocr_text))
            if q.ocr_chars > 0:
                q.rapid_coverage = q.pure_chars / q.ocr_chars
            q.rapid_low = q.rapid_coverage < 0.75
        except Exception:
            pass

    # === 维度 4: 极短页面 ===
    q.is_very_short = pure_chars < 200

    # === 维度 3: 结构元素缺失（只在非极短非图表页检查）===
    if not q.is_very_short and not q.has_diagram_ref and pure_chars < 1000:
        q.structure_missing = not q.has_section_header

    return q


def compute_density_anomalies(qualities: dict[int, PageQuality]):
    """计算维度 1: 字符密度异常（相对于相邻页）"""
    pages = sorted(qualities.keys())
    for i, pg in enumerate(pages):
        q = qualities[pg]
        if q.is_very_short or q.pure_chars < 500:
            continue
        # 比较前3页和后3页的中位数
        neighbors = []
        for d in range(-3, 4):
            if d == 0:
                continue
            npg = pg + d
            if npg in qualities and not qualities[npg].is_very_short:
                neighbors.append(qualities[npg].pure_chars)
        if len(neighbors) >= 3:
            neighbors.sort()
            median = neighbors[len(neighbors) // 2]
            if median > 0 and q.pure_chars < median * 0.5:
                q.density_anomaly = True


def score_page(q: PageQuality):
    """综合评分"""
    issues = 0
    if q.density_anomaly:
        issues += 1
    if q.rapid_low:
        issues += 2  # RapidOCR 缺失权重更高
    if q.is_very_short:
        issues += 3
    if q.structure_missing:
        issues += 1
    q.score = min(issues, 3)


def generate_report(qualities: dict[int, PageQuality]) -> str:
    """生成 QA 报告"""
    all_q = sorted(qualities.values(), key=lambda q: q.pg)
    if not all_q:
        return "无已完成页面"

    # 评分分布
    by_score = defaultdict(list)
    for q in all_q:
        by_score[q.score].append(q)

    lines = [f"# OCR 文字疏漏检测报告 @ {time.strftime('%Y-%m-%d %H:%M')}", ""]
    lines.append(f"**总页数**: {len(all_q)}")
    lines.append(f"**评分分布**: OK={len(by_score[0])}, WARN={len(by_score[1])}, "
                 f"BAD={len(by_score[2])}, CRITICAL={len(by_score[3])}")
    lines.append("")

    # 字符量分布
    chars = [q.pure_chars for q in all_q]
    lines.append(f"**字符量**: min={min(chars)}, max={max(chars)}, "
                 f"avg={sum(chars)//len(chars)}, median={sorted(chars)[len(chars)//2]}")
    lines.append("")

    # === CRITICAL 页面 ===
    if by_score[3]:
        lines.append("## [CRITICAL] 严重疏漏")
        lines.append("| 页码 | 字符数 | 问题 |")
        lines.append("| :--- | :--- | :--- |")
        for q in sorted(by_score[3], key=lambda x: x.pg):
            issues = []
            if q.is_very_short:
                issues.append("极短页")
            if q.rapid_low:
                issues.append(f"Rapid覆盖仅{q.rapid_coverage:.0%}")
            if q.density_anomaly:
                issues.append("密度异常")
            lines.append(f"| {q.pg:04d} | {q.pure_chars} | {', '.join(issues)} |")
        lines.append("")

    # === BAD 页面 ===
    if by_score[2]:
        lines.append("## [BAD] 中度疏漏")
        lines.append("| 页码 | 字符数 | Rapid覆盖 | 图表 | 问题 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for q in sorted(by_score[2], key=lambda x: x.pg)[:20]:
            issues = []
            if q.rapid_low:
                issues.append(f"Rapid覆盖仅{q.rapid_coverage:.0%}")
            if q.density_anomaly:
                issues.append("密度异常")
            if q.structure_missing:
                issues.append("缺结构")
            lines.append(f"| {q.pg:04d} | {q.pure_chars} | "
                         f"{q.rapid_coverage:.0%} | {q.has_diagram_ref} | {', '.join(issues)} |")
        if len(by_score[2]) > 20:
            lines.append(f"| ... | 共 {len(by_score[2])} 页 | | | |")
        lines.append("")

    # === WARN 页面 ===
    if by_score[1]:
        lines.append(f"## [WARN] 轻微可疑 ({len(by_score[1])} 页)")
        # 只列前10
        shown = []
        for q in sorted(by_score[1], key=lambda x: x.pg):
            if q.density_anomaly:
                shown.append(f"- p{q.pg:04d}: 密度异常 ({q.pure_chars}c)")
            elif q.structure_missing:
                shown.append(f"- p{q.pg:04d}: 缺结构元素 ({q.pure_chars}c)")
        lines.extend(shown[:10])
        if len(shown) > 10:
            lines.append(f"- ... 共 {len(shown)} 条")
        lines.append("")

    # === 字符量分布 ===
    lines.append("## 字符量分布")
    bins = [(0, 200, "极短"), (200, 500, "短"), (500, 1000, "中短"),
            (1000, 2000, "中等"), (2000, 4000, "正常"), (4000, 99999, "长")]
    for lo, hi, label in bins:
        cnt = sum(1 for q in all_q if lo <= q.pure_chars < hi)
        pct = f"{cnt/len(all_q)*100:.1f}%"
        bar = "█" * (cnt // 5)
        lines.append(f"- **{label}** ({lo}-{hi}c): {cnt} 页 ({pct}) {bar}")
    lines.append("")

    # === 内容类型 ===
    lines.append("## 内容类型覆盖")
    lines.append(f"- 含图表引用: {sum(1 for q in all_q if q.has_diagram_ref)} 页")
    lines.append(f"- 含表格: {sum(1 for q in all_q if q.has_table)} 页")
    lines.append(f"- 含公式: {sum(1 for q in all_q if q.has_formula)} 页")
    lines.append(f"- 含代码: {sum(1 for q in all_q if q.has_code)} 页")
    lines.append(f"- 含章节标题: {sum(1 for q in all_q if q.has_section_header)} 页")
    lines.append("")

    lines.append("---")
    lines.append("*自动生成。运行 `python source-material/detect_omissions.py` 刷新*")

    return "\n".join(lines)


# ==================== 修复功能 ====================

def submit_layout_job(img_path: Path) -> Optional[str]:
    """提交 layout-parsing 模式的 OCR 任务"""
    try:
        # 用 curl 上传（避免 Python SSL 问题）
        r = subprocess.run([
            "curl", "-sSL",
            "-H", f"Authorization: bearer {TOKEN}",
            "-F", "model=PaddleOCR-VL-1.6",
            "-F", f'optionalPayload={{"useLayoutDetection":true,"useDocOrientationClassify":false,"useDocUnwarping":false,"useChartRecognition":false}}',
            "-F", f"file=@{img_path}",
            JOB_URL,
        ], capture_output=True, timeout=120)
        if r.returncode == 0:
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))
            return data["data"]["jobId"]
    except Exception as e:
        print(f"    上传失败: {e}")
    return None


def poll_job(job_id: str, interval: int = 3, max_wait: int = 300) -> Optional[dict]:
    """轮询直到完成"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = subprocess.run(
            ["curl", "-sSL", "-H", f"Authorization: bearer {TOKEN}",
             f"{JOB_URL}/{job_id}"],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            time.sleep(interval)
            continue
        try:
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))["data"]
        except Exception:
            time.sleep(interval)
            continue

        state = data["state"]
        if state == "done":
            return data
        elif state == "failed":
            return None
        time.sleep(interval)
    return None


def download_result(data: dict) -> Optional[str]:
    """下载 OCR 结果（含图片）"""
    try:
        json_url = data["resultUrl"]["jsonUrl"]
    except KeyError:
        return None

    r = subprocess.run(["curl", "-sSL", json_url], capture_output=True, timeout=60)
    if r.returncode != 0:
        return None

    try:
        raw = r.stdout.decode("utf-8", errors="replace")
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        result = json.loads(lines[0])["result"]
    except Exception:
        return None

    md_parts = []
    for res in result.get("layoutParsingResults", []):
        md_text = res["markdown"]["text"]
        md_parts.append(md_text)

        # 下载图片
        imgs = res["markdown"].get("images", {})
        imgs_dir = OUTPUT_DIR / "imgs"
        imgs_dir.mkdir(parents=True, exist_ok=True)
        for fname, url in imgs.items():
            basename = fname.replace("imgs/", "").split("/")[-1]
            outpath = imgs_dir / basename
            if outpath.exists() and outpath.stat().st_size > 0:
                continue
            subprocess.run(
                ["curl", "-sSL", "-o", str(outpath), url],
                capture_output=True, timeout=30,
            )

    return "\n\n".join(md_parts)


def fix_page(pg: int) -> bool:
    """用 layout-parsing 模式重新处理单页"""
    label = f"p{pg:04d}"
    img_path = IMG_DIR / f"page_{pg:04d}.png"
    if not img_path.exists():
        print(f"  [{label}] 图片缺失")
        return False

    print(f"  [{label}] 重新处理 ({img_path.stat().st_size/1024:.0f}KB)...", end=" ", flush=True)

    job_id = submit_layout_job(img_path)
    if not job_id:
        print("提交失败")
        return False

    print(f"job={job_id[-12:]} polling...", end=" ", flush=True)
    data = poll_job(job_id)
    if not data:
        print("轮询失败")
        return False

    md_text = download_result(data)
    if not md_text:
        print("结果为空")
        return False

    # 检查改善
    new_chars = len(re.sub(r"<[^>]+>", "", md_text))
    new_chars = len(re.sub(r"\s+", "", new_chars))

    old_path = PURE_DIR / f"page_{pg:04d}.md"
    old_chars = 0
    if old_path.exists():
        with open(old_path, encoding="utf-8") as f:
            old_raw = f.read()
        old_text = re.sub(r"<[^>]+>", "", old_raw)
        old_chars = len(re.sub(r"\s+", "", old_text))

    # 只有改善才保存
    if new_chars > old_chars * 1.1 or old_chars == 0:
        with open(old_path, "w", encoding="utf-8") as f:
            f.write(md_text)
        print(f"OK! {old_chars}c → {new_chars}c (+{new_chars-old_chars})")
        return True
    else:
        print(f"SKIP (无改善: {old_chars}c → {new_chars}c)")
        return False


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="OCR 文字疏漏检测与修复")
    parser.add_argument("--fix", action="store_true", help="修复低质量页面")
    parser.add_argument("--report-only", action="store_true", help="仅生成报告（跳过完整分析）")
    parser.add_argument("--parallel", type=int, default=1, help="修复并发数")
    parser.add_argument("--limit", type=int, default=0, help="修复数量上限")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    # 1. 分析所有已完成页面
    print("[SCAN] 扫描已完成页面...")
    qualities: dict[int, PageQuality] = {}
    for pg in range(0, 775):
        q = analyze_page(pg)
        if q:
            qualities[pg] = q

    print(f"  已扫描 {len(qualities)} 页")

    # 2. 计算密度异常
    compute_density_anomalies(qualities)

    # 3. 评分
    for q in qualities.values():
        score_page(q)

    # 4. 生成报告
    report = generate_report(qualities)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  报告已保存 → {REPORT_FILE}")
    print()

    # 打印摘要
    by_score = defaultdict(list)
    for q in qualities.values():
        by_score[q.score].append(q)
    print(f"  评分: OK={len(by_score[0])}  WARN={len(by_score[1])}  "
          f"BAD={len(by_score[2])}  CRITICAL={len(by_score[3])}")

    # 5. 如果需要修复
    if args.fix:
        fix_candidates = sorted(by_score[3] + by_score[2], key=lambda q: q.pure_chars)
        # 去重
        seen = set()
        fix_list = []
        for q in fix_candidates:
            if q.pg not in seen:
                seen.add(q.pg)
                fix_list.append(q.pg)

        if args.limit > 0:
            fix_list = fix_list[:args.limit]

        print(f"\n[FIX] 准备修复 {len(fix_list)} 页: {fix_list[:10]}...")
        t0 = time.time()
        ok = 0

        if args.parallel > 1:
            with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = {pool.submit(fix_page, pg): pg for pg in fix_list}
                for fut in as_completed(futures):
                    if fut.result():
                        ok += 1
        else:
            for i, pg in enumerate(fix_list):
                print(f"[{i+1}/{len(fix_list)}]", end="")
                if fix_page(pg):
                    ok += 1

        elapsed = time.time() - t0
        print(f"\n[DONE] 修复 {ok}/{len(fix_list)} 页, 耗时 {elapsed:.0f}s")

        # 重新生成报告
        if ok > 0:
            print("\n[REFRESH] 重新生成报告...")
            qualities2 = {}
            for pg in range(0, 775):
                q = analyze_page(pg)
                if q:
                    qualities2[pg] = q
            compute_density_anomalies(qualities2)
            for q in qualities2.values():
                score_page(q)
            report2 = generate_report(qualities2)
            with open(REPORT_FILE, "w", encoding="utf-8") as f:
                f.write(report2)
            print(f"  报告已更新 → {REPORT_FILE}")
    else:
        # 只检测，列出修复建议
        fix_needed = sorted(by_score[3] + by_score[2], key=lambda q: q.pure_chars)
        if fix_needed:
            seen = set()
            print(f"\n[HINT] {len(fix_needed)} 页可能需要修复。运行 --fix 重新处理:")
            for q in fix_needed[:10]:
                if q.pg not in seen:
                    seen.add(q.pg)
                    issues = []
                    if q.is_very_short:
                        issues.append("极短")
                    if q.rapid_low:
                        issues.append(f"Rapid覆盖{q.rapid_coverage:.0%}")
                    if q.density_anomaly:
                        issues.append("密度异常")
                    print(f"  p{q.pg:04d}: {q.pure_chars}c [{', '.join(issues)}]")
            if len(fix_needed) > 10:
                print(f"  ... 共 {len(set(q.pg for q in fix_needed))} 页")
            print(f"\n  运行: python source-material/detect_omissions.py --fix --parallel 4")


if __name__ == "__main__":
    main()
