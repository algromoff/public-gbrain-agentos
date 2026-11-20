"""Child process that holds the FastEmbed model for the ingest worker.

Protocol over stdin/stdout, one JSON document per line:
    request  ["text 1", "text 2", ...]
    response [[0.1, ...], [0.2, ...]]   or   {"error": "..."}

The parent kills this process when the queue is idle, so the model's
~2 GB go back to the system instead of sitting in RAM and swap all day
(task 868: 1-2 documents a day kept the model loaded around the clock).
"""
import json
import os
import sys


def main() -> None:
    model_name = sys.argv[1]
    # Protocol gets its own copy of stdout; fd 1 then points to stderr, so a
    # stray print or progress bar from a library cannot break the protocol.
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    from fastembed import TextEmbedding

    # The unit sets FASTEMBED_CACHE_DIR, recall-mcp FASTEMBED_CACHE_PATH; without
    # cache_dir fastembed looks in /tmp, which ProtectSystem=strict makes
    # read-only, and the child dies trying to download (08.11.2026, job 338).
    cache_dir = os.environ.get("FASTEMBED_CACHE_PATH") or os.environ.get("FASTEMBED_CACHE_DIR")
    model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    for line in sys.stdin:
        try:
            texts = json.loads(line)
            vectors = [vec.tolist() for vec in model.embed(texts)]
            out.write(json.dumps(vectors) + "\n")
        except Exception as exc:  # report, keep serving
            out.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
