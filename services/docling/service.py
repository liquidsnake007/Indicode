from pathlib import Path
import hashlib
import json
import shutil

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TesseractCliOcrOptions,
)
from docling.document_converter import (
    DocumentConverter,
    PdfFormatOption,
)


app = FastAPI(
    title="Indicode Document Processing Service"
)

CACHE_DIR = Path("/cache")
UPLOAD_DIR = Path("/workspace/inputs")

CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True
)

UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# Explicitly enable OCR for PDFs.
pdf_options = PdfPipelineOptions()

pdf_options.do_ocr = True

pdf_options.ocr_options = TesseractCliOcrOptions()


converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=pdf_options
        )
    }
)


def cache_key(path: Path) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as f:

        while True:

            block = f.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


@app.get("/health")
def health():

    return {
        "status": "healthy",
        "service": "docling"
    }


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(...)
):

    filename = Path(file.filename).name

    upload_path = UPLOAD_DIR / filename

    try:

        with upload_path.open("wb") as f:

            shutil.copyfileobj(
                file.file,
                f
            )

        key = cache_key(upload_path)

        cache_path = CACHE_DIR / f"{key}.json"

        # Return cached result.
        if cache_path.exists():

            with cache_path.open(
                "r",
                encoding="utf-8"
            ) as f:

                result = json.load(f)

            result["from_cache"] = True

            return JSONResponse(
                content=result
            )

        # Parse.
        converted = converter.convert(
            str(upload_path)
        )

        document = converted.document

        markdown = document.export_to_markdown()

        result = {
            "filename": filename,
            "cache_key": key,
            "markdown": markdown,
            "from_cache": False
        }

        with cache_path.open(
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                result,
                f,
                indent=2,
                ensure_ascii=False
            )

        return JSONResponse(
            content=result
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )
