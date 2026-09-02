import httpx
from openai import OpenAI
from flashrank import Ranker, RerankRequest
from qdrant_client import QdrantClient
from config import (
    COLLECTION_NAME,
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    QDRANT_URL,
    EMBEDDING_MODEL,
)

qdrant = QdrantClient(url=QDRANT_URL)
llm = OpenAI(
    base_url=LITELLM_BASE_URL,
    api_key=LITELLM_API_KEY,
)
ranker = Ranker(
    model_name="ms-marco-MiniLM-L-12-v2",
    cache_dir="/app/flashrank_cache",
)

def embed_query(query: str):
    response = llm.embeddings.create(
        model=EMBEDDING_MODEL,
        input=query,
    )
    return response.data[0].embedding

def retrieve(
    query: str,
    top_k: int = 20,
    top_n: int = 5,
):
    vector = embed_query(query)
    response = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=top_k,
        with_payload=True,
    )
    
    candidates = []
    for point in response.points:
        candidates.append(
            {
                "id": str(point.id),
                "text": point.payload["text"],
                "source": point.payload["source_file"],
                "filename": point.payload["filename"],
                "chunk_index": int(point.payload["chunk_index"]), # Force native int
                "vector_score": float(point.score),               # Force native float
            }
        )
        
    if not candidates:
        return {
            "query": query,
            "results": [],
            "sources": [],
            "context": "",
        }
        
    passages = [
        {
            "id": index,
            "text": item["text"],
        }
        for index, item in enumerate(candidates)
    ]
    
    rerank_request = RerankRequest(
        query=query,
        passages=passages,
    )
    reranked = ranker.rerank(rerank_request)
    
    final = []
    for item in reranked[:top_n]:
        # FlashRank can also return numpy types for id and score
        original = candidates[int(item["id"])] 
        final.append(
            {
                **original,
                "rerank_score": float(item["score"]), # Force native float
            }
        )
        
    context = "\n\n---\n\n".join(
        f"[Source: {item['filename']} "
        f"| Chunk: {item['chunk_index']}]\n"
        f"{item['text']}"
        for item in final
    )
    
    return {
        "query": query,
        "results": final,
        "sources": list(
            dict.fromkeys(
                item["filename"]
                for item in final
            )
        ),
        "context": context,
    }