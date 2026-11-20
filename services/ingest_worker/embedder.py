"""FastEmbed wrapper with LRU cache.

The model lives in a child process (embed_child.py) that is started on the
first embed() and stopped after IDLE_UNLOAD_SEC without work - task 868: the
queue gets 1-2 documents a day, the model takes ~2 GB of RAM and swap.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

BATCH_SIZE = 32
CACHE_MAX_SIZE = 512
IDLE_UNLOAD_SEC = int(os.environ.get("GBRAIN_EMBED_IDLE_UNLOAD_SEC", "600"))


def _cache_key(text: str) -> str:
    """Compute short sha256 key for cache lookup."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class Embedder:
    """Batch text embedder backed by FastEmbed with in-memory cache.

    Model is loaded lazily in a child process on the first call to embed()
    or embed_single() and unloaded by release_if_idle().
    Cache uses OrderedDict as LRU with maxsize check.
    """

    def __init__(self, model_name: str = "intfloat/multilingual-e5-large") -> None:
        self._model_name = model_name
        self._child: subprocess.Popen | None = None
        self._last_used = 0.0
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def _ensure_child(self) -> subprocess.Popen:
        """Start the model process on first use."""
        if self._child is not None and self._child.poll() is None:
            return self._child
        self._child = None
        logger.info("Loading FastEmbed model: %s", self._model_name)
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-m", "services.ingest_worker.embed_child", self._model_name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        line = child.stdout.readline()
        if not line or json.loads(line).get("ready") is not True:
            child.kill()
            child.wait()
            raise RuntimeError(f"embed child failed to start: {line!r}")
        logger.info("FastEmbed model loaded in %.1f s (pid %d)",
                    time.monotonic() - started, child.pid)
        self._child = child
        return child

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        child = self._ensure_child()
        try:
            child.stdin.write(json.dumps(texts) + "\n")
            child.stdin.flush()
            line = child.stdout.readline()
        except (BrokenPipeError, OSError):
            self.close()
            raise
        if not line:
            self.close()
            raise RuntimeError("embed child exited")
        reply = json.loads(line)
        if isinstance(reply, dict):
            raise RuntimeError(f"embed child error: {reply.get('error')}")
        if len(reply) != len(texts):
            raise RuntimeError(f"embed child returned {len(reply)} vectors for {len(texts)} texts")
        return reply

    def release_if_idle(self) -> bool:
        """Stop the model process after IDLE_UNLOAD_SEC without work."""
        if self._child is None or time.monotonic() - self._last_used < IDLE_UNLOAD_SEC:
            return False
        self.close()
        logger.info("FastEmbed model unloaded after %d s idle", IDLE_UNLOAD_SEC)
        return True

    def close(self) -> None:
        """Stop the model process if it runs."""
        child, self._child = self._child, None
        if child is None:
            return
        try:
            child.stdin.close()
            child.wait(timeout=10)
        except Exception:
            child.kill()
            child.wait()

    def _cache_get(self, text: str) -> list[float] | None:
        """Get cached embedding, updating LRU order."""
        key = _cache_key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, text: str, embedding: list[float]) -> None:
        """Store embedding in cache, evicting oldest if full."""
        key = _cache_key(text)
        self._cache[key] = embedding
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed texts, using cache where possible.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors in same order as input.
        """
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            all_embeddings: list[list[float]] = []
            for batch_start in range(0, len(uncached_texts), BATCH_SIZE):
                batch = uncached_texts[batch_start:batch_start + BATCH_SIZE]
                all_embeddings.extend(self._embed_batch(batch))
                self._last_used = time.monotonic()

            for i, (idx, emb) in enumerate(
                zip(uncached_indices, all_embeddings)
            ):
                results[idx] = emb
                self._cache_put(uncached_texts[i], emb)

        return [r for r in results if r is not None]

    def embed_single(self, text: str) -> list[float]:
        """Embed a single text string.

        Args:
            text: Text to embed.

        Returns:
            Embedding vector.
        """
        return self.embed([text])[0]
