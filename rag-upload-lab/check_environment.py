"""检查依赖、CPU 配置及本地 BGE tokenizer；不修改 OGX 或 vLLM。"""
import argparse
import json
from pathlib import Path
from importlib.metadata import version

from settings import TOKENIZER


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parse", type=Path, help="可选：实际解析一个文件，PDF 首次运行可能下载模型")
    args = parser.parse_args()
    import torch
    from transformers import AutoTokenizer
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    for name in ("fastapi", "docling", "docling-core", "python-docx", "transformers", "torch"):
        print(name, version(name))
    if torch.version.cuda is not None:
        raise RuntimeError("请在 .venv-ingest 安装 CPU 版 PyTorch")
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True, trust_remote_code=False)
    print("BGE tokenizer:", TOKENIZER, "测试 token 数:", len(tokenizer.encode("电脑换新政策")))
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
    DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    from rapidocr import RapidOCR
    import onnxruntime
    print("OCR runtime:", onnxruntime.__version__)
    print("CPU 文档解析依赖与 PDF/OCR 配置检查通过")
    if args.parse:
        from parsing import parse
        blocks = parse(args.parse)
        print(json.dumps(blocks[:5], ensure_ascii=False, indent=2))
        print("结构块数量:", len(blocks))


if __name__ == "__main__":
    main()
