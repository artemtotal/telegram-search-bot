"""Short-lived process entry point for isolated ONNX embedding inference."""

import json
import sys

from user_jobs.local_embeddings import embed_documents


def main() -> None:
    texts = json.load(sys.stdin)
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        raise ValueError("expected a JSON list of strings")
    json.dump(embed_documents(texts), sys.stdout)


if __name__ == "__main__":
    main()