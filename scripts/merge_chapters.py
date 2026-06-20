#!/usr/bin/env python3
"""
按章节合并 OCR 输出页面。

功能：
  1. 扫描已完成页面的章节标题，自动检测章节边界
  2. 清理页眉（页码 + 章节名/部名）
  3. 清理页脚（页码 + 部名，如果出现在最后一行的独立行）
  4. 合并同章页面为独立 markdown 文件
  5. 生成目录索引

使用：
  python source-material/merge_chapters.py              # 干跑，显示章节边界
  python source-material/merge_chapters.py --merge       # 执行合并
"""
import os, re, sys, io, argparse, json
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from config import OUTPUT_DIR as PURE_DIR, MERGED_DIR
except ImportError:
    PURE_DIR = Path("source-material/.ocr-output")
    MERGED_DIR = PURE_DIR / "merged"
CHAPTER_PATTERN = re.compile(r"^第([一二三四五六七八九十\d]+)章\s+(.+)")

# 需要清理的行模式
PAGE_HEADER = re.compile(r"^\d{1,3}\s+(第[一二三四五六七八九十\d]+章|第一部分|第二部分|第三部分|附录)")
PART_FOOTER = re.compile(r"^\d{1,3}\s+(第一部分|第二部分|第三部分)\s*$")
BLANK_PAGE = re.compile(r"^\s*$")


def is_front_matter_line(line: str) -> bool:
    """判断是否目录/前言页的内容（粗略）"""
    return bool(re.match(r"^(目录|前言|作者简介|译者序|出版者的话|关于本书)", line.strip()))


def is_page_footer(line: str, is_last_line: bool) -> bool:
    """判断是否页脚（独立的一行页码+部名）"""
    if not is_last_line:
        return False
    return bool(PART_FOOTER.match(line.strip()))


def is_page_header(line: str) -> bool:
    """判断是否页眉（行首为页码+章节信息）"""
    stripped = line.strip()
    # 纯页码（章节首页的 "第X章 XXX 页码"）
    if re.match(r"^第[一二三四五六七八九十\d]+章\s+.+\s+\d{1,3}$", stripped):
        return True
    return bool(PAGE_HEADER.match(stripped))


def extract_chapter_number(title: str) -> tuple:
    """提取章节号（用于排序），返回 (num, is_appendix)"""
    m = CHAPTER_PATTERN.match(title)
    if not m:
        return (999, False)
    num_str = m.group(1)
    # 中文数字转整数
    zh_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    try:
        num = int(num_str)
    except ValueError:
        num = zh_map.get(num_str, 0)
    return (num, False)


def clean_page(text: str, is_chapter_start: bool) -> str:
    """清理单页内容：移除页眉页脚"""
    lines = text.split("\n")
    cleaned = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # 跳过空行
        if not stripped:
            cleaned.append(line)
            continue

        # 页眉检测
        if is_page_header(stripped):
            if is_chapter_start:
                # 章节首页：保留章节标题但去掉页码
                cleaned_line = re.sub(r"\s+\d{1,3}$", "", stripped)
                cleaned.append(cleaned_line)
            # 非章节首页的页眉：跳过
            continue

        # 页脚检测（最后一行）
        if i == len(lines) - 1 and is_page_footer(stripped, True):
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


def scan_chapters(completed_pages: list[int]) -> list[dict]:
    """扫描所有已完成页面，检测章节边界（只跟踪章号变化，不是每页都当新章）"""
    chapters = []  # [{start_pg, end_pg, title, chapter_num}]
    current = None

    zh_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
              "十一": 11, "十二": 12}

    def get_ch_num(num_str):
        try:
            return int(num_str)
        except ValueError:
            return zh_map.get(num_str, 0)

    for pg in sorted(completed_pages):
        f = PURE_DIR / f"page_{pg:04d}.md"
        if not f.exists():
            continue
        with open(f, encoding="utf-8") as fh:
            first_line = fh.readline().strip()

        # 检测「第X章 标题 页码」格式
        m = re.match(r"^第([一二三四五六七八九十\d]+)章\s+(.+?)\s+(\d{1,3})$", first_line)
        if m:
            ch_num = get_ch_num(m.group(1))
            ch_title = f"第{m.group(1)}章 {m.group(2)}"

            # 新章节出现：章号变了
            if current is None or ch_num != current["chapter_num"]:
                if current:
                    current["end_pg"] = pg - 1
                    current["pages"] = [p for p in range(current["start_pg"], pg)
                                        if p in completed_pages]
                    chapters.append(current)
                current = {
                    "start_pg": pg,
                    "chapter_num": ch_num,
                    "title": ch_title,
                    "pages": [],
                }

    # 最后一章
    if current:
        current["end_pg"] = sorted(completed_pages)[-1]
        current["pages"] = [p for p in range(current["start_pg"], current["end_pg"] + 1)
                            if p in completed_pages]
        chapters.append(current)

    return chapters


def get_front_matter_pages(completed_pages: set) -> list[int]:
    """获取正文前的页面（目录、前言等）"""
    # 找到第一章起始页
    ch1_start = None
    for pg in sorted(completed_pages):
        f = PURE_DIR / f"page_{pg:04d}.md"
        if f.exists():
            with open(f, encoding="utf-8") as fh:
                first = fh.readline().strip()
            if re.match(r"^第[1一]章\s", first):
                ch1_start = pg
                break
    if ch1_start is None:
        ch1_start = 40  # 估计
    front = [p for p in sorted(completed_pages) if p < ch1_start]
    return front


def merge_chapter(chapter: dict) -> str:
    """合并一章的所有页面为 markdown"""
    parts = []
    parts.append(f"# {chapter['title']}\n")

    for pg in chapter["pages"]:
        f = PURE_DIR / f"page_{pg:04d}.md"
        if not f.exists():
            continue
        with open(f, encoding="utf-8") as fh:
            raw = fh.read()

        is_start = (pg == chapter["start_pg"])
        cleaned = clean_page(raw, is_start)
        if cleaned.strip():
            parts.append(cleaned)

    return "\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="按章节合并 OCR 输出页面")
    parser.add_argument("--merge", action="store_true", help="执行合并（默认仅预览）")
    parser.add_argument("--chapters", type=str, default="",
                        help="指定章节（逗号分隔，如 1,2,3）")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    # 收集已完成页面
    completed = sorted(
        int(f.stem.split("_")[1])
        for f in PURE_DIR.glob("page_*.md")
        if f.stat().st_size > 50
    )
    print(f"已完成页面: {len(completed)} (p{min(completed):04d} - p{max(completed):04d})")

    # 扫描章节
    chapters = scan_chapters(completed)
    print(f"\n检测到 {len(chapters)} 章:\n")

    # 更新页码范围
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            ch["end_pg"] = chapters[i + 1]["start_pg"] - 1
            ch["pages"] = [p for p in range(ch["start_pg"], ch["end_pg"] + 1)
                           if p in completed]

    for ch in chapters:
        pg_count = len([p for p in ch["pages"] if p in completed])
        print(f"  Ch{ch['chapter_num']:2d}: {ch['title'][:40]:40s} "
              f"p{ch['start_pg']:04d}-p{ch['end_pg']:04d} ({pg_count} 页)")

    # 前页
    front = get_front_matter_pages(set(completed))
    print(f"\n  前页（目录等）: p{min(front):04d}-p{max(front):04d} ({len(front)} 页)")

    if not args.merge:
        print("\n[DRY RUN] 使用 --merge 执行合并")
        return

    # 执行合并
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    # 合并前页
    if front:
        front_parts = []
        for pg in front:
            f = PURE_DIR / f"page_{pg:04d}.md"
            if f.exists():
                with open(f, encoding="utf-8") as fh:
                    front_parts.append(fh.read())
        with open(MERGED_DIR / "00_front_matter.md", "w", encoding="utf-8") as f:
            f.write("# 前言与目录\n\n")
            f.write("\n\n".join(front_parts))
        print(f"  → merged/00_front_matter.md ({len(front)} 页)")

    # 合并各章
    filter_chapters = set()
    if args.chapters:
        filter_chapters = {int(c.strip()) for c in args.chapters.split(",")}

    total_merged = 0
    for ch in chapters:
        if filter_chapters and ch["chapter_num"] not in filter_chapters:
            continue
        ch_text = merge_chapter(ch)
        ch_file = MERGED_DIR / f"ch{ch['chapter_num']:02d}.md"
        with open(ch_file, "w", encoding="utf-8") as f:
            f.write(ch_text)
        size_kb = ch_file.stat().st_size / 1024
        print(f"  → merged/ch{ch['chapter_num']:02d}.md "
              f"({size_kb:.0f}KB, {len(ch['pages'])} 页)")
        total_merged += len(ch['pages'])

    # 生成索引
    index_lines = ["# CSAPP 第三版 — OCR 笔记索引\n", ""]
    index_lines.append(f"> 生成时间: {__import__('time').strftime('%Y-%m-%d %H:%M')}\n")
    index_lines.append(f"> 已合并: {total_merged} 页\n\n")
    for ch in chapters:
        pg_count = len(ch["pages"])
        ch_file = f"ch{ch['chapter_num']:02d}.md"
        index_lines.append(f"- [{ch['title']}]({ch_file}) ({pg_count} 页)")
    index_lines.append("")

    with open(MERGED_DIR / "README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(index_lines))

    print(f"\n[DONE] 全部合并完成 → {MERGED_DIR}")


if __name__ == "__main__":
    main()
