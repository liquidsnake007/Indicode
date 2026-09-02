from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ingest import (
    ensure_collection,
    ingest_document,
)

from retrieve import retrieve


app = FastAPI(
    title="Indicode RAG Service"
)


class IngestRequest(BaseModel):

    file_path: str


class SearchRequest(BaseModel):

    query: str


@app.on_event("startup")
def startup():

    ensure_collection()


@app.get("/health")
def health():

    return {
        "status": "healthy",
        "service": "rag",
    }


@app.post("/ingest")
def ingest(req: IngestRequest):

    try:

        return ingest_document(
            req.file_path
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


@app.post("/search")
def search(req: SearchRequest):

    try:

        return retrieve(
            req.query
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
