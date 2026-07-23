from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from functools import lru_cache


UNKNOWN_EMBEDDER_LIMIT = 4096


def _capability(
    *,
    provider: str,
    model: str,
    display_name: str,
    verified: bool,
    safe_max_chunk_length: int,
    recommended_anythingllm_limit: int,
    embedding_length,
    source_note: str,
    limit_kind: str,
):
    return {
        "provider": provider,
        "model": model,
        "display_name": display_name,
        "verified": verified,
        "safe_max_chunk_length": safe_max_chunk_length,
        "recommended_anythingllm_limit": recommended_anythingllm_limit,
        "embedding_length": embedding_length,
        "source_note": source_note,
        "limit_kind": limit_kind,
    }


ANYTHINGLLM_NATIVE_EMBEDDER_CAPABILITIES = {
    "all-minilm-l6-v2": _capability(
        provider="anythingllm",
        model="all-MiniLM-L6-v2",
        display_name="AnythingLLM Embedder: all-MiniLM-L6-v2",
        verified=True,
        safe_max_chunk_length=256,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="AnythingLLM built-in all-MiniLM-L6-v2 aligns with the sentence-transformers family, which is short-context and best kept compact.",
        limit_kind="verified_family",
    ),
    "nomic-embed-text-v1": _capability(
        provider="anythingllm",
        model="nomic-embed-text-v1",
        display_name="AnythingLLM Embedder: nomic-embed-text-v1",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="Nomic's model card describes nomic-embed-text-v1 as an 8192-token long-context embedder.",
        limit_kind="verified_family",
    ),
    "nomic-embed-text-v1.5": _capability(
        provider="anythingllm",
        model="nomic-embed-text-v1.5",
        display_name="AnythingLLM Embedder: nomic-embed-text-v1.5",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="Nomic's v1.5 family is long-context, but some local runtimes default lower unless explicitly reconfigured.",
        limit_kind="verified_family",
    ),
    "multilingual-e5-small": _capability(
        provider="anythingllm",
        model="multilingual-e5-small",
        display_name="AnythingLLM Embedder: multilingual-e5-small",
        verified=True,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=384,
        source_note="The multilingual-e5-small model card states that long texts are truncated to 512 tokens.",
        limit_kind="verified_family",
    ),
}


OPENAI_COMPATIBLE_EMBEDDER_CAPABILITIES = {
    "text-embedding-ada-002": _capability(
        provider="openai",
        model="text-embedding-ada-002",
        display_name="OpenAI: Text Embedding Ada 002",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=1536,
        source_note="OpenAI's embeddings API reference documents an 8191-token per-input limit for text-embedding-ada-002.",
        limit_kind="verified_family",
    ),
    "text-embedding-3-small": _capability(
        provider="openai",
        model="text-embedding-3-small",
        display_name="OpenAI: Text Embedding 3 Small",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=1536,
        source_note="OpenAI's embedding v3 models share the 8191-token per-input contract on the current embeddings API.",
        limit_kind="verified_family",
    ),
    "text-embedding-3-large": _capability(
        provider="openai",
        model="text-embedding-3-large",
        display_name="OpenAI: Text Embedding 3 Large",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=3072,
        source_note="OpenAI's embedding v3 models share the 8191-token per-input contract on the current embeddings API.",
        limit_kind="verified_family",
    ),
}


GEMINI_EMBEDDER_CAPABILITIES = {
    "gemini-embedding-001": _capability(
        provider="gemini",
        model="gemini-embedding-001",
        display_name="Gemini: Gemini Embedding 001",
        verified=True,
        safe_max_chunk_length=20000,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter currently advertises Gemini Embedding 001 at 20K context; the localhost app caps the AnythingLLM default lower for safer ingestion and simpler parity checks.",
        limit_kind="verified_provider_capped",
    ),
    "gemini-embedding-2": _capability(
        provider="gemini",
        model="gemini-embedding-2",
        display_name="Gemini: Gemini Embedding 2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter documents Gemini Embedding 2 with 8192-token context and flexible output dimensions up to 3072.",
        limit_kind="verified_family",
    ),
    "gemini-embedding-2-preview": _capability(
        provider="gemini",
        model="gemini-embedding-2-preview",
        display_name="Gemini: Gemini Embedding 2 Preview",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter documents Gemini Embedding 2 Preview with 8192-token context and flexible output dimensions up to 3072.",
        limit_kind="verified_family",
    ),
}


MISTRAL_EMBEDDER_CAPABILITIES = {
    "mistral-embed-2312": _capability(
        provider="mistral",
        model="mistral-embed-2312",
        display_name="Mistral: Mistral Embed 2312",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="Mistral Embed 2312 is an 8K-context text embedder. The app uses the full advertised input window unless the underlying model family is known to truncate earlier.",
        limit_kind="verified_family",
    ),
    "codestral-embed-2505": _capability(
        provider="mistral",
        model="codestral-embed-2505",
        display_name="Mistral: Codestral Embed 2505",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=None,
        source_note="Codestral Embed 2505 is exposed as an 8K-context embedder. The app uses the full advertised input window for model-aware chunk policy.",
        limit_kind="verified_family",
    ),
}


COHERE_EMBEDDER_CAPABILITIES = {
    "embed-v4.0": _capability(
        provider="cohere",
        model="embed-v4.0",
        display_name="Cohere: Embed v4.0",
        verified=True,
        safe_max_chunk_length=128000,
        recommended_anythingllm_limit=128000,
        embedding_length=1536,
        source_note="Cohere documents embed-v4.0 with a 128K context window and output dimensions 256, 512, 1024, or 1536.",
        limit_kind="verified_family",
    ),
    "embed-english-v3.0": _capability(
        provider="cohere",
        model="embed-english-v3.0",
        display_name="Cohere: Embed English v3.0",
        verified=True,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Cohere's v3 English embed family is a shorter-context text embedder and should stay on compact chunks.",
        limit_kind="verified_family",
    ),
    "embed-multilingual-v3.0": _capability(
        provider="cohere",
        model="embed-multilingual-v3.0",
        display_name="Cohere: Embed Multilingual v3.0",
        verified=True,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Cohere's multilingual v3 embed family is best treated as a compact-chunk model.",
        limit_kind="verified_family",
    ),
}


VOYAGE_EMBEDDER_CAPABILITIES = {
    "voyage-4-large": _capability(
        provider="voyage",
        model="voyage-4-large",
        display_name="Voyage AI: voyage-4-large",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-4-large with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-4": _capability(
        provider="voyage",
        model="voyage-4",
        display_name="Voyage AI: voyage-4",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-4 with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-4-lite": _capability(
        provider="voyage",
        model="voyage-4-lite",
        display_name="Voyage AI: voyage-4-lite",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-4-lite with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-3-large": _capability(
        provider="voyage",
        model="voyage-3-large",
        display_name="Voyage AI: voyage-3-large",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-3-large with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-3.5": _capability(
        provider="voyage",
        model="voyage-3.5",
        display_name="Voyage AI: voyage-3.5",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-3.5 with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-3.5-lite": _capability(
        provider="voyage",
        model="voyage-3.5-lite",
        display_name="Voyage AI: voyage-3.5-lite",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-3.5-lite with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
    "voyage-code-3": _capability(
        provider="voyage",
        model="voyage-code-3",
        display_name="Voyage AI: voyage-code-3",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=1024,
        source_note="Voyage documents voyage-code-3 with a 32K context window and default 1024-dimension embeddings.",
        limit_kind="verified_family",
    ),
}


JINA_EMBEDDER_CAPABILITIES = {
    "jina-embeddings-v4": _capability(
        provider="jinaai",
        model="jina-embeddings-v4",
        display_name="Jina AI: jina-embeddings-v4",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=2048,
        source_note="Jina documents jina-embeddings-v4 with up to 32K tokens. The app uses the full window for model-aware chunk policy.",
        limit_kind="verified_family",
    ),
    "jina-embeddings-v3": _capability(
        provider="jinaai",
        model="jina-embeddings-v3",
        display_name="Jina AI: jina-embeddings-v3",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="Jina documents jina-embeddings-v3 with an 8K context window.",
        limit_kind="verified_family",
    ),
    "jina-embeddings-v5-text-nano": _capability(
        provider="jinaai",
        model="jina-embeddings-v5-text-nano",
        display_name="Jina AI: jina-embeddings-v5-text-nano",
        verified=True,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=768,
        source_note="Jina documents jina-embeddings-v5-text-nano with a 32K context window and 768 dimensions.",
        limit_kind="verified_family",
    ),
}


PORTABLE_CURATED_EMBEDDER_CAPABILITIES = {
    "all-MiniLM-L6-v2": _capability(
        provider="portable",
        model="all-MiniLM-L6-v2",
        display_name="Portable: all-MiniLM-L6-v2",
        verified=False,
        safe_max_chunk_length=256,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="Curated portable registry entry for MiniLM deployments exposed through Generic OpenAI-compatible runtimes or local embedding gateways.",
        limit_kind="curated_portable_registry",
    ),
    "all-MiniLM-L12-v2": _capability(
        provider="portable",
        model="all-MiniLM-L12-v2",
        display_name="Portable: all-MiniLM-L12-v2",
        verified=False,
        safe_max_chunk_length=256,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="Curated portable registry entry for MiniLM L12 deployments exposed through Generic OpenAI-compatible runtimes or local embedding gateways.",
        limit_kind="curated_portable_registry",
    ),
    "paraphrase-MiniLM-L6-v2": _capability(
        provider="portable",
        model="paraphrase-MiniLM-L6-v2",
        display_name="Portable: paraphrase-MiniLM-L6-v2",
        verified=False,
        safe_max_chunk_length=256,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="Curated portable registry entry for compact paraphrase-MiniLM deployments.",
        limit_kind="curated_portable_registry",
    ),
    "all-mpnet-base-v2": _capability(
        provider="portable",
        model="all-mpnet-base-v2",
        display_name="Portable: all-mpnet-base-v2",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="Curated portable registry entry for MPNet retrieval deployments.",
        limit_kind="curated_portable_registry",
    ),
    "multi-qa-mpnet-base-dot-v1": _capability(
        provider="portable",
        model="multi-qa-mpnet-base-dot-v1",
        display_name="Portable: multi-qa-mpnet-base-dot-v1",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="Curated portable registry entry for MPNet QA retrieval deployments.",
        limit_kind="curated_portable_registry",
    ),
    "BAAI/bge-small-en-v1.5": _capability(
        provider="portable",
        model="BAAI/bge-small-en-v1.5",
        display_name="Portable: BAAI bge-small-en-v1.5",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=384,
        source_note="Curated portable registry entry for the BGE v1.5 small family.",
        limit_kind="curated_portable_registry",
    ),
    "BAAI/bge-base-en-v1.5": _capability(
        provider="portable",
        model="BAAI/bge-base-en-v1.5",
        display_name="Portable: BAAI bge-base-en-v1.5",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="Curated portable registry entry for the BGE v1.5 base family.",
        limit_kind="curated_portable_registry",
    ),
    "BAAI/bge-large-en-v1.5": _capability(
        provider="portable",
        model="BAAI/bge-large-en-v1.5",
        display_name="Portable: BAAI bge-large-en-v1.5",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Curated portable registry entry for the BGE v1.5 large family.",
        limit_kind="curated_portable_registry",
    ),
    "BAAI/bge-m3": _capability(
        provider="portable",
        model="BAAI/bge-m3",
        display_name="Portable: BAAI bge-m3",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="Curated portable registry entry for bge-m3 when it is exposed through Generic OpenAI-compatible runtimes instead of OpenRouter.",
        limit_kind="curated_portable_registry",
    ),
    "intfloat/e5-base-v2": _capability(
        provider="portable",
        model="intfloat/e5-base-v2",
        display_name="Portable: intfloat e5-base-v2",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="Curated portable registry entry for E5 base deployments.",
        limit_kind="curated_portable_registry",
    ),
    "intfloat/e5-large-v2": _capability(
        provider="portable",
        model="intfloat/e5-large-v2",
        display_name="Portable: intfloat e5-large-v2",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Curated portable registry entry for E5 large deployments.",
        limit_kind="curated_portable_registry",
    ),
    "intfloat/multilingual-e5-large": _capability(
        provider="portable",
        model="intfloat/multilingual-e5-large",
        display_name="Portable: intfloat multilingual-e5-large",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Curated portable registry entry for multilingual E5 deployments.",
        limit_kind="curated_portable_registry",
    ),
    "thenlper/gte-base": _capability(
        provider="portable",
        model="thenlper/gte-base",
        display_name="Portable: thenlper gte-base",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="Curated portable registry entry for GTE base deployments.",
        limit_kind="curated_portable_registry",
    ),
    "thenlper/gte-large": _capability(
        provider="portable",
        model="thenlper/gte-large",
        display_name="Portable: thenlper gte-large",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="Curated portable registry entry for GTE large deployments.",
        limit_kind="curated_portable_registry",
    ),
    "mixedbread-ai/mxbai-embed-large-v1": _capability(
        provider="portable",
        model="mixedbread-ai/mxbai-embed-large-v1",
        display_name="Portable: mixedbread-ai mxbai-embed-large-v1",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Curated portable registry entry for mixedbread mxbai embed deployments.",
        limit_kind="curated_portable_registry",
    ),
    "snowflake/snowflake-arctic-embed-m-v1.5": _capability(
        provider="portable",
        model="snowflake/snowflake-arctic-embed-m-v1.5",
        display_name="Portable: snowflake arctic embed m v1.5",
        verified=False,
        safe_max_chunk_length=512,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="Curated portable registry entry for Snowflake Arctic Embed deployments.",
        limit_kind="curated_portable_registry",
    ),
    "nomic-ai/nomic-embed-text-v1": _capability(
        provider="portable",
        model="nomic-ai/nomic-embed-text-v1",
        display_name="Portable: nomic-ai nomic-embed-text-v1",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="Curated portable registry entry for long-context Nomic deployments.",
        limit_kind="curated_portable_registry",
    ),
    "nomic-ai/nomic-embed-text-v1.5": _capability(
        provider="portable",
        model="nomic-ai/nomic-embed-text-v1.5",
        display_name="Portable: nomic-ai nomic-embed-text-v1.5",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="Curated portable registry entry for long-context Nomic v1.5 deployments.",
        limit_kind="curated_portable_registry",
    ),
    "Qwen/Qwen3-Embedding-0.6B": _capability(
        provider="portable",
        model="Qwen/Qwen3-Embedding-0.6B",
        display_name="Portable: Qwen Qwen3 Embedding 0.6B",
        verified=False,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=1024,
        source_note="Curated portable registry entry for Qwen3 Embedding 0.6B deployments.",
        limit_kind="curated_portable_registry",
    ),
    "Qwen/Qwen3-Embedding-4B": _capability(
        provider="portable",
        model="Qwen/Qwen3-Embedding-4B",
        display_name="Portable: Qwen Qwen3 Embedding 4B",
        verified=False,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=2560,
        source_note="Curated portable registry entry for Qwen3 Embedding 4B deployments.",
        limit_kind="curated_portable_registry",
    ),
    "Qwen/Qwen3-Embedding-8B": _capability(
        provider="portable",
        model="Qwen/Qwen3-Embedding-8B",
        display_name="Portable: Qwen Qwen3 Embedding 8B",
        verified=False,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=4096,
        source_note="Curated portable registry entry for Qwen3 Embedding 8B deployments.",
        limit_kind="curated_portable_registry",
    ),
    "jinaai/jina-embeddings-v3": _capability(
        provider="portable",
        model="jinaai/jina-embeddings-v3",
        display_name="Portable: jinaai jina-embeddings-v3",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="Curated portable registry entry for Jina Embeddings v3 deployments.",
        limit_kind="curated_portable_registry",
    ),
    "jinaai/jina-embeddings-v4": _capability(
        provider="portable",
        model="jinaai/jina-embeddings-v4",
        display_name="Portable: jinaai jina-embeddings-v4",
        verified=False,
        safe_max_chunk_length=32000,
        recommended_anythingllm_limit=32000,
        embedding_length=2048,
        source_note="Curated portable registry entry for Jina Embeddings v4 deployments.",
        limit_kind="curated_portable_registry",
    ),
    "ibm/granite-embedding-107m-multilingual": _capability(
        provider="portable",
        model="ibm/granite-embedding-107m-multilingual",
        display_name="Portable: IBM granite-embedding-107m-multilingual",
        verified=False,
        safe_max_chunk_length=2048,
        recommended_anythingllm_limit=2048,
        embedding_length=384,
        source_note="Curated portable registry entry for IBM Granite 107M multilingual embedding deployments.",
        limit_kind="curated_portable_registry",
    ),
    "ibm/granite-embedding-278m-multilingual": _capability(
        provider="portable",
        model="ibm/granite-embedding-278m-multilingual",
        display_name="Portable: IBM granite-embedding-278m-multilingual",
        verified=False,
        safe_max_chunk_length=2048,
        recommended_anythingllm_limit=2048,
        embedding_length=768,
        source_note="Curated portable registry entry for IBM Granite 278M multilingual embedding deployments.",
        limit_kind="curated_portable_registry",
    ),
}


OPENROUTER_EMBEDDER_CAPABILITIES = {
    "google/gemini-embedding-2": _capability(
        provider="openrouter",
        model="google/gemini-embedding-2",
        display_name="OpenRouter: Google Gemini Embedding 2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter documents Gemini Embedding 2 with 8192-token context and flexible output dimensions up to 3072.",
        limit_kind="verified_family",
    ),
    "google/gemini-embedding-2-preview": _capability(
        provider="openrouter",
        model="google/gemini-embedding-2-preview",
        display_name="OpenRouter: Google Gemini Embedding 2 Preview",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter documents Gemini Embedding 2 Preview with 8192-token context and flexible output dimensions up to 3072.",
        limit_kind="verified_family",
    ),
    "google/gemini-embedding-001": _capability(
        provider="openrouter",
        model="google/gemini-embedding-001",
        display_name="OpenRouter: Google Gemini Embedding 001",
        verified=True,
        safe_max_chunk_length=20000,
        recommended_anythingllm_limit=8192,
        embedding_length=3072,
        source_note="OpenRouter documents Gemini Embedding 001 with 20K context; the app caps the AnythingLLM recommendation lower for more stable ingestion and simulation parity.",
        limit_kind="verified_provider_capped",
    ),
    "openai/text-embedding-ada-002": _capability(
        provider="openrouter",
        model="openai/text-embedding-ada-002",
        display_name="OpenRouter: OpenAI Text Embedding Ada 002",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=1536,
        source_note="OpenAI's embedding API contract applies when OpenRouter proxies this model.",
        limit_kind="verified_family",
    ),
    "openai/text-embedding-3-small": _capability(
        provider="openrouter",
        model="openai/text-embedding-3-small",
        display_name="OpenRouter: OpenAI Text Embedding 3 Small",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=1536,
        source_note="OpenAI's embedding API contract applies when OpenRouter proxies this model.",
        limit_kind="verified_family",
    ),
    "openai/text-embedding-3-large": _capability(
        provider="openrouter",
        model="openai/text-embedding-3-large",
        display_name="OpenRouter: OpenAI Text Embedding 3 Large",
        verified=True,
        safe_max_chunk_length=8191,
        recommended_anythingllm_limit=8191,
        embedding_length=3072,
        source_note="OpenAI's embedding API contract applies when OpenRouter proxies this model.",
        limit_kind="verified_family",
    ),
    "perplexity/pplx-embed-v1-4b": _capability(
        provider="openrouter",
        model="perplexity/pplx-embed-v1-4b",
        display_name="OpenRouter: Perplexity Embed V1 4B",
        verified=True,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=None,
        source_note="OpenRouter documents Perplexity Embed V1 4B with 32K context.",
        limit_kind="verified_family",
    ),
    "perplexity/pplx-embed-v1-0.6b": _capability(
        provider="openrouter",
        model="perplexity/pplx-embed-v1-0.6b",
        display_name="OpenRouter: Perplexity Embed V1 0.6B",
        verified=True,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=None,
        source_note="OpenRouter documents Perplexity Embed V1 0.6B with 32K context.",
        limit_kind="verified_family",
    ),
    "nvidia/llama-nemotron-embed-vl-1b-v2:free": _capability(
        provider="openrouter",
        model="nvidia/llama-nemotron-embed-vl-1b-v2:free",
        display_name="OpenRouter Free: NVIDIA Llama Nemotron Embed VL 1B V2",
        verified=True,
        safe_max_chunk_length=131072,
        recommended_anythingllm_limit=8192,
        embedding_length=None,
        source_note="OpenRouter documents the free Nemotron embedder with 131K context; the app keeps the AnythingLLM default much lower because this project is text-PDF oriented, not multimodal vision-RAG.",
        limit_kind="verified_provider_capped",
    ),
    "thenlper/gte-base": _capability(
        provider="openrouter",
        model="thenlper/gte-base",
        display_name="OpenRouter: Thenlper GTE-Base",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=768,
        source_note="OpenRouter exposes GTE-Base with an 8K context window. The app uses the full provider-advertised input limit for model-aware chunk policy.",
        limit_kind="verified_family",
    ),
    "thenlper/gte-large": _capability(
        provider="openrouter",
        model="thenlper/gte-large",
        display_name="OpenRouter: Thenlper GTE-Large",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="OpenRouter exposes GTE-Large with an 8K context window. The app uses the full provider-advertised input limit for model-aware chunk policy.",
        limit_kind="verified_family",
    ),
    "intfloat/e5-base-v2": _capability(
        provider="openrouter",
        model="intfloat/e5-base-v2",
        display_name="OpenRouter: Intfloat E5-Base-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="OpenRouter exposes E5-Base-v2 with 8K context, but the E5 family model cards still describe 512-token truncation; the app keeps the recommendation at 512.",
        limit_kind="verified_provider_capped",
    ),
    "intfloat/e5-large-v2": _capability(
        provider="openrouter",
        model="intfloat/e5-large-v2",
        display_name="OpenRouter: Intfloat E5-Large-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="OpenRouter exposes E5-Large-v2 with 8K context, but the E5 family model cards still describe 512-token truncation; the app keeps the recommendation at 512.",
        limit_kind="verified_provider_capped",
    ),
    "intfloat/multilingual-e5-large": _capability(
        provider="openrouter",
        model="intfloat/multilingual-e5-large",
        display_name="OpenRouter: Intfloat Multilingual E5 Large",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="OpenRouter exposes Multilingual-E5-Large with 8K context, but the original multilingual E5 family still truncates long inputs at 512 tokens; the app keeps the recommendation compact.",
        limit_kind="verified_provider_capped",
    ),
    "sentence-transformers/paraphrase-minilm-l6-v2": _capability(
        provider="openrouter",
        model="sentence-transformers/paraphrase-minilm-l6-v2",
        display_name="OpenRouter: Sentence Transformers paraphrase-MiniLM-L6-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="OpenRouter exposes paraphrase-MiniLM-L6-v2 with 8K context, but the MiniLM family is still best used on short chunks.",
        limit_kind="verified_provider_capped",
    ),
    "sentence-transformers/all-minilm-l6-v2": _capability(
        provider="openrouter",
        model="sentence-transformers/all-minilm-l6-v2",
        display_name="OpenRouter: Sentence Transformers all-MiniLM-L6-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="OpenRouter exposes all-MiniLM-L6-v2 with 8K context, but the MiniLM family is still best used on short chunks.",
        limit_kind="verified_provider_capped",
    ),
    "sentence-transformers/all-minilm-l12-v2": _capability(
        provider="openrouter",
        model="sentence-transformers/all-minilm-l12-v2",
        display_name="OpenRouter: Sentence Transformers all-MiniLM-L12-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=256,
        embedding_length=384,
        source_note="OpenRouter exposes all-MiniLM-L12-v2 with 8K context, but the MiniLM family is still best used on short chunks.",
        limit_kind="verified_provider_capped",
    ),
    "sentence-transformers/all-mpnet-base-v2": _capability(
        provider="openrouter",
        model="sentence-transformers/all-mpnet-base-v2",
        display_name="OpenRouter: Sentence Transformers all-mpnet-base-v2",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="OpenRouter exposes all-mpnet-base-v2 with 8K context, but the MPNet sentence-transformer family is usually best used with medium-short chunks for retrieval.",
        limit_kind="verified_provider_capped",
    ),
    "sentence-transformers/multi-qa-mpnet-base-dot-v1": _capability(
        provider="openrouter",
        model="sentence-transformers/multi-qa-mpnet-base-dot-v1",
        display_name="OpenRouter: Sentence Transformers multi-qa-mpnet-base-dot-v1",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="The MPNet QA embedding family is better served by medium-short retrieval chunks than by very long documents.",
        limit_kind="verified_provider_capped",
    ),
    "baai/bge-base-en-v1.5": _capability(
        provider="openrouter",
        model="baai/bge-base-en-v1.5",
        display_name="OpenRouter: BAAI bge-base-en-v1.5",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=768,
        source_note="The BGE v1.5 family is exposed widely on OpenRouter, but this app still keeps the AnythingLLM recommendation modest until it has a model-specific runtime probe.",
        limit_kind="conservative_provider_family",
    ),
    "baai/bge-large-en-v1.5": _capability(
        provider="openrouter",
        model="baai/bge-large-en-v1.5",
        display_name="OpenRouter: BAAI bge-large-en-v1.5",
        verified=False,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=512,
        embedding_length=1024,
        source_note="The BGE v1.5 family is exposed widely on OpenRouter, but this app still keeps the AnythingLLM recommendation modest until it has a model-specific runtime probe.",
        limit_kind="conservative_provider_family",
    ),
    "baai/bge-m3": _capability(
        provider="openrouter",
        model="baai/bge-m3",
        display_name="OpenRouter: BAAI bge-m3",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="OpenRouter documents bge-m3 as a long-context multilingual embedder; this is a good high-capacity default for large document chunks.",
        limit_kind="verified_family",
    ),
    "mistralai/mistral-embed-2312": _capability(
        provider="openrouter",
        model="mistralai/mistral-embed-2312",
        display_name="OpenRouter: Mistral Embed 2312",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=1024,
        source_note="OpenRouter documents Mistral Embed 2312 as an 8K-context text embedder.",
        limit_kind="verified_family",
    ),
    "mistralai/codestral-embed-2505": _capability(
        provider="openrouter",
        model="mistralai/codestral-embed-2505",
        display_name="OpenRouter: Mistral Codestral Embed 2505",
        verified=True,
        safe_max_chunk_length=8192,
        recommended_anythingllm_limit=8192,
        embedding_length=None,
        source_note="OpenRouter documents Codestral Embed 2505 as an 8K-context embedder.",
        limit_kind="verified_family",
    ),
    "qwen/qwen3-embedding-8b": _capability(
        provider="openrouter",
        model="qwen/qwen3-embedding-8b",
        display_name="OpenRouter: Qwen Qwen3 Embedding 8B",
        verified=True,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=4096,
        source_note="The Qwen3 embedding family supports 32K context with configurable output dimensions. The app uses the full advertised input window.",
        limit_kind="verified_family",
    ),
    "qwen/qwen3-embedding-4b": _capability(
        provider="openrouter",
        model="qwen/qwen3-embedding-4b",
        display_name="OpenRouter: Qwen Qwen3 Embedding 4B",
        verified=True,
        safe_max_chunk_length=32768,
        recommended_anythingllm_limit=32768,
        embedding_length=2560,
        source_note="The Qwen3 embedding family supports 32K context with configurable output dimensions. The app uses the full advertised input window.",
        limit_kind="verified_family",
    ),
}


PROVIDER_LABELS = {
    "anythingllm": "AnythingLLM",
    "built-in": "AnythingLLM",
    "default": "AnythingLLM",
    "native": "AnythingLLM",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "generic-openai": "Generic OpenAI",
    "azure-openai": "Azure OpenAI",
    "gemini": "Gemini",
    "mistral": "Mistral",
    "cohere": "Cohere",
    "voyage": "Voyage AI",
    "voyageai": "Voyage AI",
    "jinaai": "Jina AI",
    "litellm": "LiteLLM",
    "lmstudio": "LM Studio",
    "lm-studio": "LM Studio",
    "localai": "LocalAI",
    "lemonade": "Lemonade",
    "ollama": "Ollama",
}

OPENAI_COMPATIBLE_PORTABLE_PROVIDERS = {
    "generic-openai",
    "litellm",
    "lmstudio",
    "lm-studio",
    "localai",
    "lemonade",
}


PORTABLE_EMBEDDER_SOURCE_MODELS = [
    ("anythingllm", "all-minilm-l6-v2"),
    ("anythingllm", "nomic-embed-text-v1"),
    ("anythingllm", "nomic-embed-text-v1.5"),
    ("anythingllm", "multilingual-e5-small"),
    ("openai", "text-embedding-ada-002"),
    ("openai", "text-embedding-3-small"),
    ("openai", "text-embedding-3-large"),
    ("gemini", "gemini-embedding-001"),
    ("gemini", "gemini-embedding-2"),
    ("gemini", "gemini-embedding-2-preview"),
    ("mistral", "mistral-embed-2312"),
    ("mistral", "codestral-embed-2505"),
    ("cohere", "embed-v4.0"),
    ("cohere", "embed-english-v3.0"),
    ("cohere", "embed-multilingual-v3.0"),
    ("voyage", "voyage-4-large"),
    ("voyage", "voyage-4"),
    ("voyage", "voyage-4-lite"),
    ("voyage", "voyage-3-large"),
    ("voyage", "voyage-3.5"),
    ("voyage", "voyage-3.5-lite"),
    ("voyage", "voyage-code-3"),
    ("jinaai", "jina-embeddings-v4"),
    ("jinaai", "jina-embeddings-v3"),
    ("jinaai", "jina-embeddings-v5-text-nano"),
    ("openrouter", "baai/bge-base-en-v1.5"),
    ("openrouter", "baai/bge-large-en-v1.5"),
    ("openrouter", "baai/bge-m3"),
    ("openrouter", "intfloat/e5-base-v2"),
    ("openrouter", "intfloat/e5-large-v2"),
    ("openrouter", "intfloat/multilingual-e5-large"),
    ("openrouter", "mistralai/codestral-embed-2505"),
    ("openrouter", "mistralai/mistral-embed-2312"),
    ("openrouter", "nvidia/llama-nemotron-embed-vl-1b-v2:free"),
    ("openrouter", "openai/text-embedding-ada-002"),
    ("openrouter", "openai/text-embedding-3-small"),
    ("openrouter", "openai/text-embedding-3-large"),
    ("openrouter", "perplexity/pplx-embed-v1-0.6b"),
    ("openrouter", "perplexity/pplx-embed-v1-4b"),
    ("openrouter", "qwen/qwen3-embedding-4b"),
    ("openrouter", "qwen/qwen3-embedding-8b"),
    ("openrouter", "sentence-transformers/all-minilm-l12-v2"),
    ("openrouter", "sentence-transformers/all-minilm-l6-v2"),
    ("openrouter", "sentence-transformers/all-mpnet-base-v2"),
    ("openrouter", "sentence-transformers/multi-qa-mpnet-base-dot-v1"),
    ("openrouter", "sentence-transformers/paraphrase-minilm-l6-v2"),
    ("openrouter", "thenlper/gte-base"),
    ("openrouter", "thenlper/gte-large"),
]


OPENROUTER_MODEL_ALIASES = {
    "google: gemini embedding 2": "google/gemini-embedding-2",
    "google: gemini embedding 2 preview": "google/gemini-embedding-2-preview",
    "google: gemini embedding 001": "google/gemini-embedding-001",
    "openai: text embedding ada 002": "openai/text-embedding-ada-002",
    "openai: text embedding 3 small": "openai/text-embedding-3-small",
    "openai: text embedding 3 large": "openai/text-embedding-3-large",
    "perplexity: embed v1 4b": "perplexity/pplx-embed-v1-4b",
    "perplexity: embed v1 0.6b": "perplexity/pplx-embed-v1-0.6b",
    "nvidia: llama nemotron embed vl 1b v2 (free)": "nvidia/llama-nemotron-embed-vl-1b-v2:free",
    "thenlper: gte-base": "thenlper/gte-base",
    "thenlper: gte-large": "thenlper/gte-large",
    "intfloat: e5-large-v2": "intfloat/e5-large-v2",
    "intfloat: e5-base-v2": "intfloat/e5-base-v2",
    "intfloat: multilingual-e5-large": "intfloat/multilingual-e5-large",
    "sentence transformers: paraphrase-minilm-l6-v2": "sentence-transformers/paraphrase-minilm-l6-v2",
    "sentence transformers: all-minilm-l12-v2": "sentence-transformers/all-minilm-l12-v2",
    "sentence transformers: all-minilm-l6-v2": "sentence-transformers/all-minilm-l6-v2",
    "sentence transformers: all-mpnet-base-v2": "sentence-transformers/all-mpnet-base-v2",
    "sentence transformers: multi-qa-mpnet-base-dot-v1": "sentence-transformers/multi-qa-mpnet-base-dot-v1",
    "baai: bge-base-en-v1.5": "baai/bge-base-en-v1.5",
    "baai: bge-large-en-v1.5": "baai/bge-large-en-v1.5",
    "baai: bge-m3": "baai/bge-m3",
    "mistral: mistral embed 2312": "mistralai/mistral-embed-2312",
    "mistral: codestral embed 2505": "mistralai/codestral-embed-2505",
    "qwen: qwen3 embedding 8b": "qwen/qwen3-embedding-8b",
    "qwen: qwen3 embedding 4b": "qwen/qwen3-embedding-4b",
}


NATIVE_MODEL_ALIASES = {
    "all-minilm-l6-v2": "all-minilm-l6-v2",
    "nomic-embed-text-v1": "nomic-embed-text-v1",
    "nomic-embed-text-v1.5": "nomic-embed-text-v1.5",
    "multilingual-e5-small": "multilingual-e5-small",
}


OPENAI_MODEL_ALIASES = {
    "text embedding ada 002": "text-embedding-ada-002",
    "text embedding 3 small": "text-embedding-3-small",
    "text embedding 3 large": "text-embedding-3-large",
    "text-embedding-ada-002": "text-embedding-ada-002",
    "text-embedding-3-small": "text-embedding-3-small",
    "text-embedding-3-large": "text-embedding-3-large",
}


GEMINI_MODEL_ALIASES = {
    "gemini embedding 001": "gemini-embedding-001",
    "gemini embedding 2": "gemini-embedding-2",
    "gemini embedding 2 preview": "gemini-embedding-2-preview",
    "gemini-embedding-001": "gemini-embedding-001",
    "gemini-embedding-2": "gemini-embedding-2",
    "gemini-embedding-2-preview": "gemini-embedding-2-preview",
}


MISTRAL_MODEL_ALIASES = {
    "mistral embed 2312": "mistral-embed-2312",
    "codestral embed 2505": "codestral-embed-2505",
    "mistral-embed-2312": "mistral-embed-2312",
    "codestral-embed-2505": "codestral-embed-2505",
}


COHERE_MODEL_ALIASES = {
    "embed v4.0": "embed-v4.0",
    "embed-v4.0": "embed-v4.0",
    "embed english v3.0": "embed-english-v3.0",
    "embed-english-v3.0": "embed-english-v3.0",
    "embed multilingual v3.0": "embed-multilingual-v3.0",
    "embed-multilingual-v3.0": "embed-multilingual-v3.0",
}


VOYAGE_MODEL_ALIASES = {
    "voyage-4-large": "voyage-4-large",
    "voyage-4": "voyage-4",
    "voyage-4-lite": "voyage-4-lite",
    "voyage-3-large": "voyage-3-large",
    "voyage-3.5": "voyage-3.5",
    "voyage-3.5-lite": "voyage-3.5-lite",
    "voyage-code-3": "voyage-code-3",
}


JINA_MODEL_ALIASES = {
    "jina-embeddings-v4": "jina-embeddings-v4",
    "jina embeddings v4": "jina-embeddings-v4",
    "jina-embeddings-v3": "jina-embeddings-v3",
    "jina embeddings v3": "jina-embeddings-v3",
    "jina-embeddings-v5-text-nano": "jina-embeddings-v5-text-nano",
    "jina embeddings v5 text nano": "jina-embeddings-v5-text-nano",
}


OLLAMA_MODEL_FAMILY_HINTS = [
    ("snowflake-arctic-embed", 512, 512, 1024, "Snowflake Arctic Embed family fallback."),
    ("snowflake-arctic-embed2", 512, 512, 1024, "Snowflake Arctic Embed 2 family fallback."),
    ("bge-m3", 8192, 8192, 1024, "Known BGE-M3 family fallback."),
    ("bge-small-en-v1.5", 512, 512, 384, "Known BGE small family fallback."),
    ("bge-base-en-v1.5", 512, 512, 768, "Known BGE base family fallback."),
    ("bge-large-en-v1.5", 512, 512, 1024, "Known BGE large family fallback."),
    ("bge-base", 512, 512, 768, "Generic BGE base family fallback."),
    ("bge-large", 512, 512, 1024, "Generic BGE large family fallback."),
    ("bge-small", 512, 512, 384, "Generic BGE small family fallback."),
    ("embeddinggemma", 2048, 2048, 768, "EmbeddingGemma family fallback."),
    ("nomic-embed-text-v2-moe", 512, 512, 768, "Nomic embed v2 MoE family fallback."),
    ("nomic-embed-text-v1.5", 8192, 8192, 768, "Nomic long-context family fallback."),
    ("nomic-embed-text-v1", 8192, 8192, 768, "Nomic long-context family fallback."),
    ("nomic-embed-text", 8192, 8192, 768, "Nomic embed family fallback."),
    ("granite-embedding-278m", 2048, 2048, 768, "Granite embedding 278M family fallback."),
    ("granite-embedding-107m", 2048, 2048, 384, "Granite embedding 107M family fallback."),
    ("qwen3-embedding-8b", 32768, 32768, 4096, "Qwen3 Embedding 8B family fallback."),
    ("qwen3-embedding-4b", 32768, 32768, 2560, "Qwen3 Embedding 4B family fallback."),
    ("qwen3-embedding-0.6b", 32768, 32768, 1024, "Qwen3 Embedding 0.6B family fallback."),
    ("qwen3-embedding", 32768, 32768, 1024, "Qwen3 embedding family fallback."),
    ("tarka-embed", 2048, 2048, 768, "Tarka embedding family fallback."),
    ("mxbai-embed", 512, 512, 1024, "MXBAI embedding family fallback."),
    ("mxbai-embed-large", 512, 512, 1024, "MXBAI Embed Large family fallback."),
    ("multilingual-e5-base", 512, 512, 768, "Multilingual E5 base family fallback."),
    ("multilingual-e5-small", 512, 512, 384, "Multilingual E5 small family fallback."),
    ("multilingual-e5-large", 512, 512, 1024, "Multilingual E5 large family fallback."),
    ("e5-base-v2", 512, 512, 768, "E5 base family fallback."),
    ("e5-large-v2", 512, 512, 1024, "E5 large family fallback."),
    ("gte-base-en-v1.5", 8192, 8192, 768, "GTE base v1.5 family fallback."),
    ("gte-large-en-v1.5", 8192, 8192, 1024, "GTE large v1.5 family fallback."),
    ("gte-modernbert-base", 8192, 8192, 768, "GTE ModernBERT base family fallback."),
    ("gte-modernbert-large", 8192, 8192, 1024, "GTE ModernBERT large family fallback."),
    ("gte-base", 8192, 8192, 768, "GTE base family fallback."),
    ("gte-large", 8192, 8192, 1024, "GTE large family fallback."),
    ("all-minilm-l6-v2", 256, 256, 384, "MiniLM L6 family fallback."),
    ("all-minilm-l12-v2", 256, 256, 384, "MiniLM L12 family fallback."),
    ("paraphrase-minilm-l6-v2", 256, 256, 384, "Paraphrase MiniLM family fallback."),
    ("paraphrase-multilingual-mpnet-base-v2", 512, 512, 768, "Paraphrase multilingual MPNet family fallback."),
    ("all-mpnet-base-v2", 512, 512, 768, "MPNet family fallback."),
    ("multi-qa-mpnet-base-dot-v1", 512, 512, 768, "MPNet QA family fallback."),
    ("jina-embeddings-v2-base-en", 8192, 8192, 768, "Jina Embeddings v2 base English family fallback."),
    ("jina-embeddings-v2-small-en", 8192, 8192, 512, "Jina Embeddings v2 small English family fallback."),
    ("jina-embeddings-v3", 8192, 8192, 1024, "Jina Embeddings v3 family fallback."),
    ("mistral-embed", 8192, 8192, 1024, "Mistral embed family fallback."),
    ("codestral-embed", 8192, 8192, None, "Codestral embed family fallback."),
    ("pplx-embed-v1-4b", 32768, 32768, None, "Perplexity 4B family fallback."),
    ("pplx-embed-v1-0.6b", 32768, 32768, None, "Perplexity 0.6B family fallback."),
    ("voyage-4-large", 32000, 32000, 1024, "Voyage 4 Large family fallback."),
    ("voyage-4-lite", 32000, 32000, 1024, "Voyage 4 Lite family fallback."),
    ("voyage-4", 32000, 32000, 1024, "Voyage 4 family fallback."),
    ("voyage-3-large", 32000, 32000, 1024, "Voyage 3 Large family fallback."),
    ("voyage-3.5-lite", 32000, 32000, 1024, "Voyage 3.5 Lite family fallback."),
    ("voyage-3.5", 32000, 32000, 1024, "Voyage 3.5 family fallback."),
    ("voyage-code-3", 32000, 32000, 1024, "Voyage Code 3 family fallback."),
    ("embed-v4.0", 128000, 128000, 1536, "Cohere Embed v4.0 family fallback."),
    ("embed-english-v3.0", 512, 512, 1024, "Cohere Embed English v3.0 family fallback."),
    ("embed-multilingual-v3.0", 512, 512, 1024, "Cohere Embed Multilingual v3.0 family fallback."),
    ("jina-embeddings-v4", 32000, 32000, 2048, "Jina Embeddings v4 family fallback."),
    ("jina-embeddings-v5-text-nano", 32000, 32000, 768, "Jina Embeddings v5 text nano family fallback."),
]


OPENROUTER_EMBEDDINGS_MODELS_URL = "https://openrouter.ai/api/v1/embeddings/models"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


def ollama_base_url(value: str = "") -> str:
    base = (value or "").strip() or DEFAULT_OLLAMA_BASE_URL
    for suffix in ("/api/embed", "/api/embeddings", "/api/tags"):
        if base.rstrip("/").endswith(suffix):
            base = base.rstrip("/")[: -len(suffix)]
            break
    return base.rstrip("/")


def is_embedding_like_model_name(name: str) -> bool:
    lowered = (name or "").casefold()
    hints = ["embed", "embedding", "bge", "nomic", "mxbai", "e5", "gte", "jina", "snowflake", "tarka"]
    return any(hint in lowered for hint in hints)


def _openrouter_display_name(name: str, slug: str) -> str:
    base = (name or slug or "Unknown embedder").strip()
    if ": " in base:
        provider, remainder = base.split(": ", 1)
        return f"OpenRouter: {provider} {remainder}".strip()
    return f"OpenRouter: {base}".strip()


def _openrouter_runtime_aliases(row: dict) -> dict:
    aliases = {}
    slug = (row.get("id") or row.get("canonical_slug") or "").strip()
    if not slug:
        return aliases
    name = (row.get("name") or "").strip()
    hf_id = (row.get("hugging_face_id") or "").strip()
    for candidate in {
        slug,
        row.get("canonical_slug") or "",
        name,
        _openrouter_display_name(name, slug),
        hf_id,
    }:
        normalized = normalize_model_text(candidate)
        if normalized:
            aliases[normalized] = slug
    return aliases


def _openrouter_runtime_capability(row: dict) -> dict | None:
    slug = (row.get("id") or row.get("canonical_slug") or "").strip()
    if not slug:
        return None
    provider_ctx = ((row.get("top_provider") or {}).get("context_length")) or 0
    declared_ctx = row.get("context_length") or 0
    safe_limit = int(provider_ctx or declared_ctx or UNKNOWN_EMBEDDER_LIMIT)
    if provider_ctx and declared_ctx:
        safe_limit = min(int(provider_ctx), int(declared_ctx))
    architecture = row.get("architecture") or {}
    return _capability(
        provider="openrouter",
        model=slug,
        display_name=_openrouter_display_name((row.get("name") or "").strip(), slug),
        verified=True,
        safe_max_chunk_length=safe_limit,
        recommended_anythingllm_limit=safe_limit,
        embedding_length=architecture.get("embedding_length"),
        source_note="Loaded from OpenRouter's public embeddings catalog and constrained to the provider-advertised input window when available.",
        limit_kind="provider_catalog",
    )


@lru_cache(maxsize=2)
def _openrouter_runtime_catalog_cached(cache_buster: int = 0) -> tuple[dict, dict]:
    capabilities = {}
    aliases = {}
    try:
        with urllib.request.urlopen(OPENROUTER_EMBEDDINGS_MODELS_URL, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return capabilities, aliases
    for row in payload.get("data", []) or []:
        capability = _openrouter_runtime_capability(row)
        if not capability:
            continue
        slug = capability["model"]
        capabilities[slug] = capability
        aliases.update(_openrouter_runtime_aliases(row))
    return capabilities, aliases


def openrouter_runtime_catalog(force_refresh: bool = False) -> tuple[dict, dict]:
    if force_refresh:
        _openrouter_runtime_catalog_cached.cache_clear()
    return _openrouter_runtime_catalog_cached(0)


@lru_cache(maxsize=4)
def _ollama_runtime_catalog_cached(base_url: str) -> tuple[dict, list[str], str]:
    capabilities = {}
    all_models = []
    base = ollama_base_url(base_url)
    try:
        req = urllib.request.Request(f"{base}/api/tags")
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return capabilities, all_models, str(exc)
    for row in payload.get("models", []) or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        all_models.append(name)
        if not is_embedding_like_model_name(name):
            continue
        capabilities[name] = capability_for_ollama_model(name)
    return capabilities, sorted(all_models, key=str.casefold), ""


def ollama_runtime_catalog(force_refresh: bool = False, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> tuple[dict, list[str], str]:
    normalized_base = ollama_base_url(base_url)
    if force_refresh:
        _ollama_runtime_catalog_cached.cache_clear()
    return _ollama_runtime_catalog_cached(normalized_base)


def merged_openrouter_catalog(force_refresh: bool = False, include_live: bool = False) -> tuple[dict, dict]:
    runtime_capabilities = {}
    runtime_aliases = {}
    if include_live:
        runtime_capabilities, runtime_aliases = openrouter_runtime_catalog(force_refresh=force_refresh)
    merged_capabilities = dict(runtime_capabilities)
    merged_aliases = dict(runtime_aliases)
    merged_capabilities.update(OPENROUTER_EMBEDDER_CAPABILITIES)
    merged_aliases.update(OPENROUTER_MODEL_ALIASES)
    for slug in merged_capabilities:
        normalized_slug = normalize_model_text(slug)
        if normalized_slug and normalized_slug not in merged_aliases:
            merged_aliases[normalized_slug] = slug
    return merged_capabilities, merged_aliases


def openrouter_simulation_option_map(force_refresh: bool = False, include_live: bool = False):
    capabilities, _ = merged_openrouter_catalog(force_refresh=force_refresh, include_live=include_live)
    return {
        capability["display_name"]: slug
        for slug, capability in sorted(
            capabilities.items(),
            key=lambda item: item[1].get("display_name", "").casefold(),
        )
    }


def provider_catalog_entries(provider: str, force_refresh: bool = False) -> list[dict]:
    normalized = normalize_provider(provider)
    if normalized in {"anythingllm", "native", "built-in", "default"}:
        mapping = ANYTHINGLLM_NATIVE_EMBEDDER_CAPABILITIES
    elif normalized == "ollama":
        mapping, _all_models, _error = ollama_runtime_catalog(force_refresh=force_refresh)
    elif normalized == "openrouter":
        mapping, _ = merged_openrouter_catalog(force_refresh=force_refresh, include_live=True)
    elif normalized == "openai":
        mapping = OPENAI_COMPATIBLE_EMBEDDER_CAPABILITIES
    elif normalized in OPENAI_COMPATIBLE_PORTABLE_PROVIDERS:
        return [
            _clone_capability(row, provider_override=normalized)
            for row in portable_catalog_entries(force_refresh=force_refresh)
        ]
    elif normalized == "azure-openai":
        mapping = OPENAI_COMPATIBLE_EMBEDDER_CAPABILITIES
    elif normalized == "gemini":
        mapping = GEMINI_EMBEDDER_CAPABILITIES
    elif normalized == "mistral":
        mapping = MISTRAL_EMBEDDER_CAPABILITIES
    elif normalized == "cohere":
        mapping = COHERE_EMBEDDER_CAPABILITIES
    elif normalized in {"voyage", "voyageai"}:
        mapping = VOYAGE_EMBEDDER_CAPABILITIES
    elif normalized == "jinaai":
        mapping = JINA_EMBEDDER_CAPABILITIES
    else:
        return []
    return [
        _clone_capability(row, provider_override=normalized)
        for _, row in sorted(mapping.items(), key=lambda item: item[1].get("display_name", "").casefold())
    ]


def portable_catalog_entries(force_refresh: bool = False) -> list[dict]:
    entries = {}
    source_maps = {
        "anythingllm": ANYTHINGLLM_NATIVE_EMBEDDER_CAPABILITIES,
        "openai": OPENAI_COMPATIBLE_EMBEDDER_CAPABILITIES,
        "gemini": GEMINI_EMBEDDER_CAPABILITIES,
        "mistral": MISTRAL_EMBEDDER_CAPABILITIES,
        "cohere": COHERE_EMBEDDER_CAPABILITIES,
        "voyage": VOYAGE_EMBEDDER_CAPABILITIES,
        "jinaai": JINA_EMBEDDER_CAPABILITIES,
        "openrouter": OPENROUTER_EMBEDDER_CAPABILITIES,
    }
    runtime_openrouter, _ = openrouter_runtime_catalog(force_refresh=force_refresh)
    for source_provider, model in PORTABLE_EMBEDDER_SOURCE_MODELS:
        source_map = runtime_openrouter if source_provider == "openrouter" and model in runtime_openrouter else source_maps.get(source_provider, {})
        row = source_map.get(model)
        if not row:
            continue
        cloned = _clone_capability(row)
        cloned["portable_source_provider"] = source_provider
        cloned["portable_source_label"] = PROVIDER_LABELS.get(source_provider, source_provider)
        entries[normalize_model_text(cloned.get("model") or model)] = cloned
    return sorted(entries.values(), key=lambda item: item.get("display_name", "").casefold())


def provider_catalog_counts(force_refresh: bool = False) -> dict:
    providers = [
        "anythingllm",
        "ollama",
        "openai",
        "gemini",
        "mistral",
        "cohere",
        "voyage",
        "jinaai",
        "openrouter",
    ]
    counts = {}
    for provider in providers:
        counts[provider] = len(provider_catalog_entries(provider, force_refresh=force_refresh))
    counts["portable"] = len(portable_catalog_entries(force_refresh=force_refresh))
    return counts


def normalize_provider(provider: str) -> str:
    return (provider or "").strip().casefold()


def normalize_model_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _base_capability(provider: str, model: str) -> dict:
    display_provider = PROVIDER_LABELS.get(normalize_provider(provider), provider or "Unknown")
    model_text = (model or "").strip() or "not configured"
    return {
        "provider": normalize_provider(provider),
        "model": model_text,
        "display_name": f"{display_provider}: {model_text}",
        "verified": False,
        "safe_max_chunk_length": UNKNOWN_EMBEDDER_LIMIT,
        "recommended_anythingllm_limit": UNKNOWN_EMBEDDER_LIMIT,
        "embedding_length": None,
        "source_note": "Unknown model capability. Using a conservative fallback.",
        "limit_kind": "unknown",
        "status": "unknown_capability",
    }


def _clone_capability(row: dict, provider_override="") -> dict:
    cloned = dict(row)
    if provider_override:
        cloned["provider"] = normalize_provider(provider_override)
    cloned["status"] = "verified" if cloned.get("verified") else "conservative"
    return cloned


def _resolve_from_mapping(provider: str, model: str, mapping: dict, aliases: dict) -> dict | None:
    normalized_model = normalize_model_text(model)
    canonical = aliases.get(normalized_model, normalized_model)
    if canonical in mapping:
        return _clone_capability(mapping[canonical], provider_override=provider)
    return None


def _capability_from_family_hint(provider: str, model: str) -> dict | None:
    lowered = normalize_model_text(model)
    for needle, safe_limit, recommended_limit, embedding_length, note in OLLAMA_MODEL_FAMILY_HINTS:
        if needle in lowered:
            cap = _base_capability(provider, model)
            cap.update(
                {
                    "safe_max_chunk_length": safe_limit,
                    "recommended_anythingllm_limit": recommended_limit,
                    "embedding_length": embedding_length,
                    "source_note": note,
                    "limit_kind": "family_fallback",
                    "status": "conservative",
                }
            )
            return cap
    return None


def capability_for_openrouter_model(model: str, include_live: bool = False) -> dict:
    # State rendering and offline policy resolution must never trigger network
    # discovery. Explicit catalog-refresh surfaces can opt into live data.
    mapping, aliases = merged_openrouter_catalog(include_live=include_live)
    cap = _resolve_from_mapping("openrouter", model, mapping, aliases)
    if cap:
        return cap
    cap = _capability_from_family_hint("openrouter", model)
    if cap:
        return cap
    fallback = _base_capability("openrouter", model)
    fallback["display_name"] = f"OpenRouter: {(model or 'unknown').strip() or 'unknown'}"
    return fallback


def capability_for_openai_compatible_model(provider: str, model: str) -> dict:
    cap = _resolve_from_mapping(provider, model, OPENAI_COMPATIBLE_EMBEDDER_CAPABILITIES, OPENAI_MODEL_ALIASES)
    if cap:
        display_provider = PROVIDER_LABELS.get(normalize_provider(provider), provider)
        cap["display_name"] = cap["display_name"].replace("OpenAI", display_provider, 1)
        return cap
    cap = _capability_from_family_hint(provider, model)
    if cap:
        return cap
    return _base_capability(provider, model)


def capability_for_gemini_model(model: str) -> dict:
    cap = _resolve_from_mapping("gemini", model, GEMINI_EMBEDDER_CAPABILITIES, GEMINI_MODEL_ALIASES)
    if cap:
        return cap
    return _capability_from_family_hint("gemini", model) or _base_capability("gemini", model)


def capability_for_mistral_model(model: str) -> dict:
    cap = _resolve_from_mapping("mistral", model, MISTRAL_EMBEDDER_CAPABILITIES, MISTRAL_MODEL_ALIASES)
    if cap:
        return cap
    return _capability_from_family_hint("mistral", model) or _base_capability("mistral", model)


def capability_for_cohere_model(model: str) -> dict:
    cap = _resolve_from_mapping("cohere", model, COHERE_EMBEDDER_CAPABILITIES, COHERE_MODEL_ALIASES)
    if cap:
        return cap
    return _capability_from_family_hint("cohere", model) or _base_capability("cohere", model)


def capability_for_voyage_model(provider: str, model: str) -> dict:
    cap = _resolve_from_mapping(provider, model, VOYAGE_EMBEDDER_CAPABILITIES, VOYAGE_MODEL_ALIASES)
    if cap:
        return cap
    return _capability_from_family_hint(provider, model) or _base_capability(provider, model)


def capability_for_jina_model(model: str) -> dict:
    cap = _resolve_from_mapping("jinaai", model, JINA_EMBEDDER_CAPABILITIES, JINA_MODEL_ALIASES)
    if cap:
        return cap
    return _capability_from_family_hint("jinaai", model) or _base_capability("jinaai", model)


def capability_for_native_embedder(model: str = "") -> dict:
    cap = _resolve_from_mapping("anythingllm", model, ANYTHINGLLM_NATIVE_EMBEDDER_CAPABILITIES, NATIVE_MODEL_ALIASES)
    if cap:
        return cap
    base = _base_capability("anythingllm", model or "native")
    model_text = (model or "").strip()
    if model_text:
        base["display_name"] = f"AnythingLLM Embedder: {model_text}"
    else:
        base["display_name"] = "AnythingLLM native embedder"
    base["source_note"] = "AnythingLLM native embedder does not expose a dependable model-specific limit in local storage. Using a conservative fallback."
    base["recommended_anythingllm_limit"] = UNKNOWN_EMBEDDER_LIMIT
    base["safe_max_chunk_length"] = UNKNOWN_EMBEDDER_LIMIT
    base["status"] = "conservative"
    return base


def parse_ollama_show_output(text: str) -> dict:
    metadata = {
        "context_length": None,
        "embedding_length": None,
        "architecture": "",
        "capabilities": [],
    }
    if not text:
        return metadata
    if match := re.search(r"context length\s+(\d+)", text, re.I):
        metadata["context_length"] = int(match.group(1))
    if match := re.search(r"embedding length\s+(\d+)", text, re.I):
        metadata["embedding_length"] = int(match.group(1))
    if match := re.search(r"architecture\s+([^\r\n]+)", text, re.I):
        metadata["architecture"] = match.group(1).strip()
    capabilities = []
    if block_match := re.search(r"Capabilities\s+(.+?)(?:\n\s*\n|\Z)", text, re.I | re.S):
        capabilities = [line.strip() for line in block_match.group(1).splitlines() if line.strip()]
    metadata["capabilities"] = capabilities
    return metadata


@lru_cache(maxsize=128)
def inspect_ollama_model(model: str) -> dict:
    target = (model or "").strip()
    result = {
        "status": "unavailable",
        "model": target,
        "context_length": None,
        "embedding_length": None,
        "architecture": "",
        "capabilities": [],
        "error": "",
    }
    if not target:
        result["status"] = "missing_model"
        return result
    try:
        completed = subprocess.run(
            ["ollama", "show", target],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result
    if completed.returncode != 0:
        result["status"] = "error"
        result["error"] = (completed.stderr or completed.stdout or "").strip()
        return result
    metadata = parse_ollama_show_output(completed.stdout or "")
    result.update(metadata)
    result["status"] = "loaded"
    return result


def capability_for_ollama_model(model: str, include_live: bool = False) -> dict:
    key = (model or "").strip()
    cap = _base_capability("ollama", key)
    cap["display_name"] = f"Ollama: {key or 'unknown'}"
    family = _capability_from_family_hint("ollama", key)
    inspected = inspect_ollama_model(key) if include_live else {"status": "not_requested"}
    if inspected.get("status") == "loaded":
        runtime_context = int(inspected.get("context_length") or UNKNOWN_EMBEDDER_LIMIT)
        runtime_embedding_length = inspected.get("embedding_length")
        cap.update(
            {
                "verified": True,
                "safe_max_chunk_length": runtime_context,
                "recommended_anythingllm_limit": runtime_context,
                "embedding_length": runtime_embedding_length,
                "source_note": "Loaded from local `ollama show` metadata.",
                "limit_kind": "runtime_metadata",
                "status": "verified",
                "architecture": inspected.get("architecture") or "",
                "capabilities": inspected.get("capabilities") or [],
            }
        )
        if family:
            cap["recommended_anythingllm_limit"] = int(family.get("recommended_anythingllm_limit") or runtime_context)
            if family.get("embedding_length") and not runtime_embedding_length:
                cap["embedding_length"] = family["embedding_length"]
            cap["source_note"] = (
                "Loaded from local `ollama show` metadata. "
                + (family.get("source_note") or "")
            ).strip()
            cap["limit_kind"] = "runtime_metadata_plus_family_policy"
        return cap
    if family:
        return family
    return cap


def resolve_embedder_capability(provider: str, model: str) -> dict:
    normalized = normalize_provider(provider)
    if normalized == "openrouter":
        return capability_for_openrouter_model(model)
    if normalized == "ollama":
        return capability_for_ollama_model(model)
    if normalized in {"anythingllm", "native", "built-in", "default"}:
        return capability_for_native_embedder(model)
    if normalized in {"openai", "generic-openai", "azure-openai", "litellm", "lmstudio", "lm-studio", "localai", "lemonade"}:
        return capability_for_openai_compatible_model(normalized, model)
    if normalized == "gemini":
        return capability_for_gemini_model(model)
    if normalized == "mistral":
        return capability_for_mistral_model(model)
    if normalized == "cohere":
        return capability_for_cohere_model(model)
    if normalized in {"voyage", "voyageai"}:
        return capability_for_voyage_model(normalized, model)
    if normalized == "jinaai":
        return capability_for_jina_model(model)
    family = _capability_from_family_hint(normalized or "unknown", model)
    if family:
        return family
    cap = _base_capability(normalized or "unknown", model)
    cap["display_name"] = f"{PROVIDER_LABELS.get(normalized, normalized or 'Unknown')}: {(model or 'not configured')}".strip()
    return cap
