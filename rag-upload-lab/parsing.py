"""解析为结构块。PDF 使用 CPU Docling/OCR；DOCX 保留段落/表格顺序。"""
import re
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def text_blocks(text):
    headings = []
    blocks = []
    pending = []
    in_code = False

    def flush():
        if pending:
            blocks.append({"text": "\n".join(pending).strip(), "heading": " > ".join(headings),
                           "page": None, "kind": "text"})
            pending.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        match = re.match(r"^(#{1,6})\s+(.+)$", line) if not in_code else None
        if match:
            flush()
            level = len(match[1])
            headings = headings[:level - 1] + [match[2].strip()]
        elif not line.strip() and not in_code:
            flush()
        else:
            pending.append(line)
    flush()
    return blocks


def docx_blocks(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    document = Document(path)
    blocks, headings = [], []
    document_title = ""

    def heading_path():
        return " > ".join(([document_title] if document_title else []) +
                          [text for _, text in headings])

    for element in document.element.body:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name if paragraph.style else ""
            style_id = paragraph.style.style_id if paragraph.style else ""
            if style == "Title" or style_id == "Title":
                document_title = text
                headings = []
                continue
            heading = re.match(r"Heading (\d+)", style)
            if heading:
                level = int(heading[1])
                headings = [(depth, label) for depth, label in headings if depth < level]
                headings.append((level, text))
                continue
            blocks.append({"text": text, "heading": heading_path(), "page": None, "kind": "text"})
        elif tag == "tbl":
            table = Table(element, document)
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            # 每行带表头，防止分块后失去列含义；复杂合并单元格需人工检查。
            for row in rows[1:] or rows:
                text = "；".join(f"{header or f'列{i + 1}'}：{value}"
                                 for i, (header, value) in enumerate(zip(rows[0], row)))
                blocks.append({"text": text, "heading": heading_path(), "page": None, "kind": "table"})
    return blocks


def digital_pdf_blocks(path):
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling_core.types.doc import TableItem

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(path)
    if result.status != ConversionStatus.SUCCESS:
        raise RuntimeError(f"PDF 未完整解析：{result.status}; {result.errors}")
    blocks, heading = [], ""
    for item, _ in result.document.iterate_items():
        label = getattr(item.label, "value", str(item.label))
        if label in {"page_header", "page_footer"}:
            continue
        if isinstance(item, TableItem):
            text = item.export_to_markdown(doc=result.document)
            kind = "table"
        else:
            text = getattr(item, "text", "")
            kind = "text"
        if not text.strip():
            continue
        if label in {"title", "section_header"}:
            heading = text.strip()
            continue
        pages = sorted({p.page_no for p in getattr(item, "prov", [])})
        blocks.append({"text": text.strip(), "heading": heading,
                       "page": ",".join(map(str, pages)) or None, "kind": kind})
    return blocks


def pdf_blocks(path):
    import pypdfium2
    from settings import DATA
    document = pypdfium2.PdfDocument(path)
    try:
        scan_pages = []
        force = os.environ.get("RAG_PDF_FORCE_OCR") == "1"
        for number in range(len(document)):
            page = document[number]
            textpage = page.get_textpage()
            try:
                # 可手工启用整页 OCR，处理文字层损坏或图文混排遗漏。
                if force or len(textpage.get_text_range().strip()) < 20:
                    scan_pages.append(number)
            finally:
                textpage.close()
                page.close()
        blocks = digital_pdf_blocks(path) if len(scan_pages) < len(document) else []
        scan_numbers = {str(number + 1) for number in scan_pages}
        blocks = [block for block in blocks
                  if not (set(str(block.get("page", "")).split(",")) & scan_numbers)]
        if scan_pages:
            from rapidocr import RapidOCR
            engine = RapidOCR(params={
                "Global.model_root_dir": os.environ.get("RAG_OCR_CACHE", str(DATA / "models" / "rapidocr")),
                "EngineConfig.onnxruntime.use_cuda": False,
                "EngineConfig.onnxruntime.use_coreml": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            })
            for number in scan_pages:
                page = document[number]
                bitmap = page.render(scale=3)
                try:
                    rendered = bitmap.to_pil().convert("RGB")
                    result = engine(rendered)
                    low, high = rendered.convert("L").getextrema()
                finally:
                    bitmap.close()
                    page.close()
                text = "\n".join(result.txts or [])
                if text.strip():
                    blocks.append({"text": text, "heading": "", "page": str(number + 1), "kind": "ocr"})
                elif high - low > 10:
                    raise RuntimeError(f"PDF 第 {number + 1} 页非空，但 OCR 未提取到文字；请检查图片或扫描质量")
        blocks.sort(key=lambda block: int(str(block.get("page") or "0").split(",")[0]))
        return blocks
    finally:
        document.close()


def parse(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        blocks = text_blocks(path.read_text(encoding="utf-8-sig"))
    elif suffix == ".docx":
        blocks = docx_blocks(path)
    elif suffix == ".pdf":
        blocks = pdf_blocks(path)
    elif suffix == ".doc":
        binary = shutil.which("libreoffice") or shutil.which("soffice")
        if not binary:
            raise RuntimeError("旧版 .doc 需要 LibreOffice。请安装其 headless/Writer 组件，或另存为 .docx 后上传。")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            subprocess.run([binary, "-env:UserInstallation=" + (output / "profile").as_uri(),
                            "--headless", "--convert-to", "docx", "--outdir", str(output), str(path)],
                           check=True, capture_output=True, timeout=120)
            converted = output / (path.stem + ".docx")
            if not converted.is_file():
                raise RuntimeError("LibreOffice 未生成 DOCX 文件")
            blocks = docx_blocks(converted)
    else:
        raise ValueError("不支持的文件类型")
    blocks = [block for block in blocks if block["text"].strip()]
    if not blocks:
        raise RuntimeError("未提取到正文；检查空文件、加密文件或 OCR 结果")
    return blocks
