"""
note-skill 共享配置模块。

使用前修改以下变量：
  - API_TOKEN: OCR API 访问令牌
  - API_URL: OCR API 端点
  - MODEL: 使用的模型名称
"""

import os
from pathlib import Path

# ============================================================
# OCR API 配置（用户需要修改这里）
# ============================================================

API_TOKEN = os.environ.get("OCR_API_TOKEN", "your-token-here")
API_URL = os.environ.get("OCR_API_URL", "https://your-ocr-api.example.com/v2/ocr/jobs")
MODEL = os.environ.get("OCR_MODEL", "PaddleOCR-VL-1.6")

# ============================================================
# 目录结构（相对于项目根目录）
# ============================================================

# 输入
IMG_DIR = Path("source-material/.ocr-work/images")
PDF_PATH = Path("source-material/教材.pdf")

# 输出
OUTPUT_DIR = Path("source-material/.ocr-output")
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
MERGED_DIR = OUTPUT_DIR / "merged"
CLEANED_DIR = MERGED_DIR / "cleaned"
IMGS_DIR = OUTPUT_DIR / "imgs"

# ============================================================
# HTTP 请求头
# ============================================================

HEADERS = {
    "Authorization": f"bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

# ============================================================
# OCR 参数
# ============================================================

PURE_OCR_PAYLOAD = {
    "useLayoutDetection": False,
    "promptLabel": "ocr",
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}

LAYOUT_PAYLOAD = {
    "useLayoutDetection": True,
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}

# 短文本阈值：结果少于此字符数触发 layout fallback
MIN_CHAR_THRESHOLD = 200


# ============================================================
# 工具函数
# ============================================================

def ensure_dirs():
    """确保所有输出目录存在"""
    for d in [OUTPUT_DIR, MERGED_DIR, CLEANED_DIR, IMGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def valid_page(page_num: int) -> bool:
    """检查单页 OCR 结果是否有效（非空、非极小）"""
    f = OUTPUT_DIR / f"page_{page_num:04d}.md"
    return f.exists() and f.stat().st_size > 50
