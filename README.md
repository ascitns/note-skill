# note-skill

将任意知识性书籍（**扫描版 PDF** 或其他格式）内容转换为学习笔记

*这是一个面向自己的试错经历，语风和文章结构很个性化，但是可以提供一些参考*

## 快速开始

### 需要什么

1. **OCR API 访问凭据**（端点 URL + Token，paddle ocr很好用）

   *https://aistudio.baidu.com/paddleocr额度基本用不完，速度和准确率也可以*

2. **教材**

3. Claude Code以及一个便宜的llm api(推荐ds-v4-flash，学校给的qwen可以用，虽然免费但效果不佳)

4. 一个轻量的md阅读器（推荐typora因为设计和主题特别好看）

### 可以做什么

- 转一些讨厌的扫描版pdf（也许会有更优解，但是能跑就行因为重点不是这个，实际上也差不了多少）

- 最重要的其实是去AI味，作为一个**浪荡不羁桀骜不驯**的人，我最**讨厌**的就是想利用ai学习时还要被**装腔作势**地教学
- 优化了笔记**结构**和**排版**稳定性

###  怎么作

在 Claude Code 中，将教材 PDF 放入工作目录后直接说：

```
/note-skill *你的需求*，我将给你提供URL和api
```

cc将会做以下事：

1. 分段pdf（一般是50页以内）
2. 逐段上传到 OCR API，轮询直到完成
3. 下载全部 markdown + 图片
4. 按 `skill.md` 规则生成笔记
5. 输出到目录下 `source-material/merged/cleaned/`

> **需要 pip 依赖**：`requests`、`pymupdf`（Claude Code 会自动安装）

### 其他 OCR API

对于只接受单张图片的 API，Claude Code 会自动切换模式（逐页图片上传），提供 API 凭据即可，无需关心实现细节。

## 文件

```
note-skill/
├── README.md
├── skill.md
└── scripts/
    ├── config.py
    ├── ocr_upload_images.py
    ├── backfill_images.py
    ├── merge_chapters.py
    ├── cleanup_chapters.py
    ├── detect_garbled_pages.py
    ├── detect_omissions.py
    └── fix_diagram_pages.py
```

