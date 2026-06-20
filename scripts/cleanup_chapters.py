#!/usr/bin/env python3
"""
清理合并后的章节 markdown，保守修复 OCR 残留问题。
原则：只修格式，不动内容和顺序，不创小标题。

修复项：
  1. 合并断裂段落（跨页断句）
  2. 整理散落的图表标签（图 X-X 周围的碎片行收集为引用块）
  3. 去除重复章节标题
  4. 旁注格式化为 blockquote
  5. HTML <img> 转标准 markdown ![]()
  6. 清理多余空行（>2 合并为 2）
  7. 去除孤立的页码数字
  8. 代码片段检测与 ``` 包裹（汇编、shell 命令、C 代码）

使用：
  python source-material/cleanup_chapters.py --dry-run  # 预览改动
  python source-material/cleanup_chapters.py             # 执行清理
  python source-material/cleanup_chapters.py --ch 01     # 只处理一章
"""
import re, sys, io, argparse, os
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from config import MERGED_DIR, CLEANED_DIR
except ImportError:
    MERGED_DIR = Path("source-material/.ocr-output/merged")
    CLEANED_DIR = MERGED_DIR / "cleaned"

# === 规则1: 合并断裂段落 ===
# 上一条非空行末尾不是句号/问号/感叹号/右括号/冒号，且下一行是小写开头（暗示跨页断句）
SENTENCE_END = re.compile(r"[。！？）\)」』] $|[：:]\s*$")
CONTINUATION_START = re.compile(r"^[^\nA-Z\d#<\|\[\-●\*·•\s]")  # 非特殊开头，暗示是上文延续


def is_broken_continuation(current_line: str, prev_line: str) -> bool:
    """判断 current_line 是否是上一行的断裂延续"""
    prev = prev_line.strip()
    cur = current_line.strip()
    if not prev or not cur:
        return False
    # 上一行不以句号等结尾
    if SENTENCE_END.search(prev):
        return False
    # 上一行是列表项，不合并
    if re.match(r"^[-●\*·•\d+\.]\s", prev):
        return False
    # 当前行以小写字母、中文或数字开头（延续特征）
    if re.match(r"^[a-z一-鿿\d]", cur):
        return True
    # 当前行很短（1-5个中文），很可能是断裂的尾巴
    if len(cur) <= 10 and re.match(r"^[一-鿿]", cur):
        return True
    return False


def merge_broken_paragraphs(lines: list[str]) -> list[str]:
    """合并跨页断裂的段落"""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 如果下一行是当前行的延续，合并
        if i + 1 < len(lines) and is_broken_continuation(lines[i + 1], line):
            merged = line.rstrip() + lines[i + 1].lstrip()
            result.append(merged)
            i += 2
        else:
            result.append(line)
            i += 1
    return result


# === 规则2: 整理散落的图表标签 ===
# 在 "图 X-X ..." 之前的碎片行（CPU、ALU 等单行标签）收集为引用块
DIAGRAM_CAPTION = re.compile(r"^图\s*\d+[\.\-]\d+")
DIAGRAM_LABEL = re.compile(r"^[A-Z][A-Za-z/\s]{2,30}$")  # CPU, ALU, I/O 总线 等
DIAGRAM_CN_LABEL = re.compile(r"^[一-鿿]{2,12}$")  # 系统总线, 内存总线 等


def collect_diagram_labels(lines: list[str]) -> list[str]:
    """将图注周围的碎片标签收集为引用块"""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 检测图注行
        if DIAGRAM_CAPTION.match(line):
            # 向前找碎片标签（最多往前找5行）
            labels = []
            j = i - 1
            while j >= 0 and j >= i - 6:
                prev = result[j].strip()
                if prev.startswith(">") or prev.startswith("```") or not prev:
                    break
                if DIAGRAM_LABEL.match(prev) or DIAGRAM_CN_LABEL.match(prev):
                    labels.insert(0, result.pop(j))
                    j -= 1
                else:
                    break

            # 如果有标签，合并为引用块
            if labels:
                quote = "\n".join(f"> {lbl}" for lbl in labels)
                result.append(quote)
                result.append("")  # 空行分隔
                result.append(f"_{line}_")  # 图注斜体
            else:
                result.append(lines[i])
            i += 1
        else:
            result.append(lines[i])
            i += 1
    return result


# === 规则3: 去除重复章节标题 ===
CHAPTER_HEADER = re.compile(r"^#\s*(第[一二三四五六七八九十\d]+章\s+.+)")


def remove_duplicate_headers(lines: list[str]) -> list[str]:
    """如果 # 标题后紧跟同样的纯文本标题，移除重复"""
    result = []
    for i, line in enumerate(lines):
        m = CHAPTER_HEADER.match(line)
        if m:
            title = m.group(1)
            result.append(line)
            # 检查下一行是否纯文本重复
            if i + 1 < len(lines) and lines[i + 1].strip() == title:
                continue  # 跳过下一行（空行会被后续规则处理）
        else:
            result.append(line)
    return result


# === 规则4: 旁注格式化 ===
SIDE_NOTE = re.compile(r"^(旁注\s+.+)")


def format_side_notes(lines: list[str]) -> list[str]:
    """将旁注行格式化为 blockquote"""
    result = []
    in_note = False
    for line in lines:
        m = SIDE_NOTE.match(line.strip())
        if m:
            if not in_note:
                result.append("")  # 前空行
            result.append(f"> **{m.group(1)}**")
            in_note = True
        elif in_note and line.strip():
            result.append(f"> {line.strip()}")
        else:
            if in_note and not line.strip():
                result.append("")  # 结束后空行
            result.append(line)
            in_note = False
    return result


# === 规则5: HTML img → Markdown ===
IMG_TAG = re.compile(
    r'<div[^>]*>\s*<img\s+src="([^"]+)"\s+alt="([^"]*)"[^>]*/?>\s*</div>',
    re.IGNORECASE,
)
IMG_CAPTION = re.compile(
    r'<div[^>]*>\s*(图\s*\d+[\.\-]\d+[^<]*)\s*</div>',
    re.IGNORECASE,
)


def convert_img_tags(lines: list[str]) -> list[str]:
    """将 HTML img + caption div 转为标准 markdown"""
    text = "\n".join(lines)

    # 先合并 img + 紧接着的 caption
    # <div><img src="imgs/xxx.jpg"/></div>\n\n<div>图 1-6 XXX</div>
    # → ![图 1-6 XXX](imgs/xxx.jpg)
    combined = re.compile(
        r'<div[^>]*>\s*<img\s+src="([^"]+)"\s+alt="[^"]*"[^>]*/?>\s*</div>\s*\n\s*\n\s*'
        r'<div[^>]*>\s*(图\s*\d+[\.\-]\d+[^<]*)\s*</div>',
        re.IGNORECASE,
    )

    def replace_combined(m):
        return f"\n![{m.group(2)}]({m.group(1)})\n"

    text = combined.sub(replace_combined, text)

    # 再处理孤立的 img（没有紧跟 caption 的）
    def replace_img(m):
        return f"\n![图示]({m.group(1)})\n"

    text = IMG_TAG.sub(replace_img, text)

    # 清理残留的单独 caption div
    def replace_caption(m):
        return f"\n**{m.group(1)}**\n"

    text = IMG_CAPTION.sub(replace_caption, text)

    return text.split("\n")


# === 规则6: 代码块格式化 ===
ASM_INSTR = re.compile(
    r"^\s*(mov|add|sub|push|pop|call|ret|jmp|lea|cmp|test|xor|and|or|shl|shr|inc|dec|"
    r"int|nop|leave|enter|pushq|popq|movq|addq|subq|imulq|xorq|cmpq|je|jne|jg|jl|"
    r"jge|jle|ja|jb|\.L|main:|\.LC)[\s\$\%]",
    re.I,
)
LINUX_CMD = re.compile(r"^linux[>\s]")


def format_code_blocks(lines: list[str]) -> list[str]:
    """检测连续的代码行，用 ``` 包裹"""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 检测汇编代码块起始
        if ASM_INSTR.match(stripped):
            result.append("```asm")
            result.append(line)
            i += 1
            while i < len(lines):
                cur = lines[i].strip()
                if not cur:
                    result.append(lines[i])
                    i += 1
                    continue
                if ASM_INSTR.match(cur) or re.match(r"^\s*\w+:", cur):
                    result.append(lines[i])
                    i += 1
                else:
                    break
            result.append("```")
            continue

        # 检测 shell 命令块
        if LINUX_CMD.match(stripped):
            result.append("```shell")
            result.append(line)
            i += 1
            while i < len(lines):
                cur = lines[i].strip()
                if not cur:
                    result.append(lines[i])
                    i += 1
                    continue
                if LINUX_CMD.match(cur):
                    result.append(lines[i])
                    i += 1
                else:
                    break
            result.append("```")
            continue

        result.append(line)
        i += 1

    return result


# === 规则7: 清理多余空行 ===
def normalize_blank_lines(lines: list[str]) -> list[str]:
    """最多保留 2 个连续空行"""
    result = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)
    return result


# === 规则7: 去除孤立页码 ===
STRAY_PAGE_NUM = re.compile(r"^\d{1,3}$")


def remove_stray_page_numbers(lines: list[str]) -> list[str]:
    """移除孤立的页码数字行"""
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if STRAY_PAGE_NUM.match(stripped):
            # 只在它前后都有空行时移除（更安全）
            prev_empty = i == 0 or not lines[i - 1].strip()
            next_empty = i == len(lines) - 1 or not lines[i + 1].strip()
            if prev_empty and next_empty:
                continue
        result.append(line)
    return result


# === 主流程 ===

def cleanup_text(text: str) -> str:
    """执行全部清理规则"""
    lines = text.split("\n")

    # 顺序很重要
    lines = merge_broken_paragraphs(lines)     # 1. 先合并断句
    lines = remove_duplicate_headers(lines)     # 2. 去重标题
    lines = collect_diagram_labels(lines)       # 3. 整理图表标签
    lines = format_side_notes(lines)            # 4. 旁注格式化
    lines = format_code_blocks(lines)           # 5. 代码块包裹
    lines = convert_img_tags(lines)             # 6. img 转 markdown
    lines = remove_stray_page_numbers(lines)    # 7. 去页码
    lines = normalize_blank_lines(lines)        # 8. 合并空行

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="清理合并的章节 markdown")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入文件")
    parser.add_argument("--ch", type=str, default="", help="只处理指定章节（如 01,02）")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    chapters = sorted(MERGED_DIR.glob("ch*.md"))
    if args.ch:
        chapters = [f for f in chapters if f"ch{args.ch}" in f.name]

    if not chapters:
        print("无匹配的章节文件")
        return

    print(f"处理 {len(chapters)} 章:\n")

    for chf in chapters:
        with open(chf, encoding="utf-8") as f:
            original = f.read()

        cleaned = cleanup_text(original)

        # 统计变化
        orig_lines = original.count("\n")
        new_lines = cleaned.count("\n")
        diff = new_lines - orig_lines

        if args.dry_run:
            print(f"  {chf.name}: {orig_lines} → {new_lines} 行 ({diff:+d}) [预览]")
        else:
            out = CLEANED_DIR / chf.name
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(cleaned)
            size_kb = out.stat().st_size / 1024
            print(f"  {chf.name}: {orig_lines} → {new_lines} 行 ({diff:+d}) → {out}")


if __name__ == "__main__":
    main()
