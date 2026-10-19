"""The PDF parse: marker, in text mode (ADR-0017).

Text mode reads the text layer only (no OCR). marker's models (layout,
tables) run on the GPU if it is free, else on the CPU: a 5-page paper took
22 s on the GPU and 197 s on the CPU, with the same text. The ingest runs it
in a subprocess, and selects the device:

    python -m llm_engine.documents <pdf_path> [cuda|cpu]

The subprocess keeps marker's output away from the MCP server's stdout (the
protocol channel), and frees marker's RAM when the parse ends.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# A relative LOG_DIR is relative to the repo root: the MCP server can start
# in any folder.
PAPERS_DIR = Path(__file__).resolve().parents[1] / os.getenv("LOG_DIR", "logs") / "papers"

# The parse runs beside training (data loaders), so it uses half the cores.
PARSE_THREADS = max(1, (os.cpu_count() or 2) // 2)
# Measured on a 5-page paper: 4,960 MiB reserved at the peak, plus the CUDA
# context, plus a margin.
PARSE_VRAM_MIB = 6144


def parse_seconds(pdf_path: Path) -> tuple[float, float]:
    """The expected time of a parse: (on the GPU, on the CPU). Measured on
    the GPU: 22 s for 5 pages, 102 s for 23 pages and 156 s for 26 pages
    (journal papers with many tables); on the CPU: 197 s for 5 pages, of
    which about 15 s is the model load and the layout model takes 29 s for
    each page."""
    import pypdfium2
    pages = len(pypdfium2.PdfDocument(str(pdf_path)))
    return 15 + 5 * pages, 15 + 36 * pages


def first_pages(pdf_path: Path, count: int = 2) -> list[str]:
    """The text layer of the first pages of a PDF (no parse; empty for a
    scan)."""
    import pypdfium2
    pdf = pypdfium2.PdfDocument(str(pdf_path))
    try:
        return [pdf[i].get_textpage().get_text_bounded() for i in range(min(count, len(pdf)))]
    finally:
        pdf.close()

def text_path_for(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(pdf_path.suffix + ".md")


def _extract(path: Path) -> tuple[str, dict]:
    """Returns (markdown_text, images) — images maps filename -> PIL.Image,
    already what marker-pdf extracted from the PDF (figures, embedded
    pictures, etc.), empty if the document has none."""
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    config = ConfigParser({
        "disable_ocr": True,
        # A "{n}" + 48 dashes line before each page (n from 0), so that each
        # chunk knows its page (document_index.chunk_text).
        "paginate_output": True,
        # One process for the text layer; the cores go to the models.
        "pdftext_workers": 1,
    }).generate_config_dict()
    rendered = PdfConverter(config=config, artifact_dict=create_model_dict())(str(path))
    text, _ext, images = text_from_rendered(rendered)
    return text, images


def parse_pdf(pdf_path: Path) -> Path:
    """Parses pdf_path and writes the results next to it:

        {pdf}.md              <- extracted markdown, with page separators
        images/{name}         <- embedded images, if any

    Returns the path of the markdown file. CPU-heavy: run it in a subprocess
    (see the module docstring), not in the MCP server.
    """
    text, images = _extract(pdf_path)
    text_path = text_path_for(pdf_path)
    text_path.write_text(text)

    if images:
        images_dir = pdf_path.parent / "images"
        images_dir.mkdir(exist_ok=True)
        for name, image in images.items():
            image.save(images_dir / name)

    return text_path


if __name__ == "__main__":
    # Before torch loads. On the CPU: no CUDA context (no VRAM at all).
    device = sys.argv[2] if len(sys.argv) > 2 else "cpu"
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["TORCH_DEVICE"] = device
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = str(PARSE_THREADS)
    parse_pdf(Path(sys.argv[1]))
