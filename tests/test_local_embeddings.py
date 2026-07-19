import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from user_jobs import local_embeddings


class LocalEmbeddingTests(unittest.TestCase):
    def tearDown(self):
        local_embeddings._reset_model_for_tests()

    def test_document_and_query_embeddings_are_local_and_384_dimensional(self):
        class FakeEmbeddingModel:
            def embed(self, texts, batch_size):
                self.calls = (texts, batch_size)
                return [[float(i)] * 384 for i, _ in enumerate(texts, 1)]

        model = FakeEmbeddingModel()
        with patch.object(local_embeddings, "_get_model", return_value=model):
            vectors = local_embeddings.embed_documents(["электрик", "сантехник"])
            query = local_embeddings.embed_query("мастер")

        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), 384)
        self.assertEqual(len(query), 384)
        self.assertEqual(model.calls, (["мастер"], local_embeddings.LOCAL_EMBED_BATCH_SIZE))

    def test_local_model_is_loaded_without_api_credentials(self):
        class FakeTextEmbedding:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with patch.dict(os.environ, {"LOCAL_EMBED_OFFLINE": "1"}, clear=True), patch.object(
            local_embeddings, "TextEmbedding", FakeTextEmbedding,
        ):
            model = local_embeddings._get_model()

        self.assertEqual(
            model.kwargs["model_name"],
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        self.assertEqual(model.kwargs["threads"], local_embeddings.LOCAL_EMBED_THREADS)
        self.assertTrue(model.kwargs["local_files_only"])


if __name__ == "__main__":
    unittest.main()
