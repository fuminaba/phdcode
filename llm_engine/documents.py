import io
import os
import re
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv()

UPLOADS_DIR = Path(os.getenv("LOG_DIR", "logs")) / "uploads"

# marker-pdf/surya batch sizes -- unset (None) leaves surya's own CUDA
# defaults (36/32/256/32) in place. Lowered via .env to shrink extraction's
# peak VRAM spike, which can otherwise collide with Ollama on a shared GPU.
_BATCH_SIZE_ENV_TO_CONFIG_KEY = {
    "MARKER_DETECTION_BATCH_SIZE": "detection_batch_size",
    "MARKER_LAYOUT_BATCH_SIZE": "layout_batch_size",
    "MARKER_RECOGNITION_BATCH_SIZE": "recognition_batch_size",
    "MARKER_TABLE_REC_BATCH_SIZE": "table_rec_batch_size",
}

_converter = None

# Matches a run of consecutive markdown table rows (header, separator, data).
_TABLE_PATTERN = re.compile(r"((?:^\|.*\|\s*\n)+)", re.MULTILINE)


class Attachment(NamedTuple):
    filename: str
    content: bytes


class ExtractedDocument(NamedTuple):
    filename: str
    raw_path: Path
    text_path: Path
    image_paths: list[Path]
    table_paths: list[Path]


def _get_converter():
    """Lazily builds (and caches) the marker-pdf converter. Loads several ML
    models on first call — deliberately not done at import time, so just
    importing this module (or the package that pulls it in) stays cheap.
    Cached for the process lifetime: the first call pays for model loading
    (a few seconds, plus a one-time Triton JIT compile); every call after
    that is fast, and nothing is held in GPU memory between calls."""
    global _converter
    if _converter is None:
        from marker.config.parser import ConfigParser
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        batch_config = {
            config_key: int(os.environ[env_key])
            for env_key, config_key in _BATCH_SIZE_ENV_TO_CONFIG_KEY.items()
            if env_key in os.environ
        }
        # pdftext's own internal per-page parallelism (default 4 workers)
        # uses a ProcessPoolExecutor -- which a Celery prefork worker's
        # child process can't spawn (`daemonic processes are not allowed
        # to have children`; confirmed via a real ingestion run under the
        # actual worker, not guessed). Forced to 1 unconditionally: there's
        # no throughput to gain from it once extraction itself only ever
        # runs one at a time anyway, serialized through the same queue
        # this would otherwise conflict with.
        batch_config["pdftext_workers"] = 1
        config = ConfigParser(batch_config).generate_config_dict()
        _converter = PdfConverter(config=config, artifact_dict=create_model_dict())
    return _converter


def _extract(path: Path) -> tuple[str, dict]:
    """Returns (markdown_text, images) — images maps filename -> PIL.Image,
    already what marker-pdf extracted from the PDF (figures, embedded
    pictures, etc.), empty if the document has none."""
    from marker.output import text_from_rendered
    rendered = _get_converter()(str(path))
    text, _ext, images = text_from_rendered(rendered)
    return text, images


def _markdown_table_to_dataframe(markdown_table: str):
    """Parses one markdown table (header, separator, data rows) into a
    polars DataFrame. Adapted from a prior project's marker-pdf pipeline
    (~/Desktop/dev/ALiRA/core/document_processor/services/marker_pdf2md.py)."""
    import polars as pl

    raw_lines = markdown_table.strip().split("\n")
    cleaned_lines = []
    for idx, line in enumerate(raw_lines):
        if idx == 1 and "-" in line:
            continue  # the header/data separator row, e.g. |---|---|
        line = line.strip().strip("|")
        cells = [cell.strip() for cell in line.split("|")]
        cleaned_lines.append("|".join(cells))

    data_txt = "\n".join(cleaned_lines)
    return pl.read_csv(io.StringIO(data_txt), separator="|", has_header=True)


def _extract_tables(markdown_text: str) -> list[str]:
    return [t.strip() for t in _TABLE_PATTERN.findall(markdown_text)]


def ingest_document(dir_path: Path, filename: str, content: bytes) -> ExtractedDocument:
    """Saves an uploaded file into dir_path and extracts its text (marker-pdf),
    embedded images, and tables (as CSVs) alongside it:

        dir_path/
            {filename}            <- raw upload
            {filename}.md         <- extracted markdown
            images/{name}         <- embedded images, if any
            tables/{i}.csv        <- extracted tables, if any

    dir_path is created by the caller (see memory/document_index.py) using a
    document row's own id, so two uploads never collide even with the same
    filename. Synchronous and CPU/GPU-heavy — call via asyncio.to_thread from
    async callers.
    """
    dir_path.mkdir(parents=True, exist_ok=True)

    raw_path = dir_path / filename
    raw_path.write_bytes(content)

    text, images = _extract(raw_path)

    text_path = raw_path.with_suffix(raw_path.suffix + ".md")
    text_path.write_text(text)

    image_paths = []
    if images:
        images_dir = dir_path / "images"
        images_dir.mkdir(exist_ok=True)
        for name, image in images.items():
            image_path = images_dir / name
            image.save(image_path)
            image_paths.append(image_path)

    table_paths = []
    for i, table_md in enumerate(_extract_tables(text)):
        try:
            df = _markdown_table_to_dataframe(table_md)
        except Exception:
            continue  # malformed/OCR-mangled table -- skip it, don't fail the whole upload
        tables_dir = dir_path / "tables"
        tables_dir.mkdir(exist_ok=True)
        table_path = tables_dir / f"{i}.csv"
        df.write_csv(table_path)
        table_paths.append(table_path)

    return ExtractedDocument(
        filename=filename,
        raw_path=raw_path,
        text_path=text_path,
        image_paths=image_paths,
        table_paths=table_paths,
    )
