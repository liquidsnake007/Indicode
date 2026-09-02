from pathlib import Path
import hashlib

import httpx

from openai import OpenAI

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from config import (
    COLLECTION_NAME,
    DOCLING_URL,
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    QDRANT_URL,
    EMBEDDING_MODEL,
    VECTOR_SIZE,
)


qdrant = QdrantClient(
    url=QDRANT_URL
)

llm = OpenAI(
    base_url=LITELLM_BASE_URL,
    api_key=LITELLM_API_KEY,
)


def ensure_collection():

    collections = qdrant.get_collections().collections

    if not any(
        c.name == COLLECTION_NAME
        for c in collections
    ):

        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )


def chunk_text(
    text: str,
    chunk_size: int = 700,
    overlap: int = 100,
):

    chunks = []

    start = 0

    while start < len(text):

        end = min(
            start + chunk_size,
            len(text)
        )

        chunk = text[
            start:end
        ].strip()

        if chunk:
            chunks.append(chunk)

        if end == len(text):
            break

        start = end - overlap

    return chunks


def embed(text: str):

    response = llm.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text,
    )

    vector = response.data[0].embedding

    if len(vector) != VECTOR_SIZE:

        raise RuntimeError(
            f"Expected embedding dimension "
            f"{VECTOR_SIZE}, got {len(vector)}"
        )

    return vector


def deterministic_id(
    source: str,
    index: int
):

    raw = f"{source}:{index}"

    digest = hashlib.sha256(
        raw.encode()
    ).hexdigest()

    return int(
        digest[:15],
        16
    )


def parse_with_docling(
    path: Path
):

    with path.open(
        "rb"
    ) as f:

        response = httpx.post(
            f"{DOCLING_URL}/parse",
            files={
                "file": (
                    path.name,
                    f,
                    "application/octet-stream",
                )
            },
            timeout=300,
        )

    response.raise_for_status()

    return response.json()


def ingest_document(
    path_str: str
):

    path = Path(path_str)

    suffix = path.suffix.lower()

    # Native text files don't need OCR.
    if suffix in {
        ".txt",
        ".md",
    }:

        text = path.read_text(
            encoding="utf-8",
            errors="ignore",
        )

    else:

        parsed = parse_with_docling(
            path
        )

        text = parsed["markdown"]

    if not text.strip():

        raise RuntimeError(
            f"No text extracted from {path}"
        )

    chunks = chunk_text(text)

    points = []

    for index, chunk in enumerate(chunks):

        vector = embed(chunk)

        points.append(
            PointStruct(
                id=deterministic_id(
                    str(path),
                    index,
                ),
                vector=vector,
                payload={
                    "text": chunk,
                    "source_file": str(path),
                    "filename": path.name,
                    "chunk_index": index,
                },
            )
        )

    qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
    )

    return {
        "status": "success",
        "file": str(path),
        "chunks_stored": len(points),
    }
