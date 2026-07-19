FROM python:3.9-slim
WORKDIR /app

# Install system dependencies (cached unless this changes)
RUN apt update && apt install gcc -y && apt clean

# Install Python dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN /usr/local/bin/python -m pip install --upgrade pip && pip install -r requirements.txt

# Preload the local multilingual ONNX model so runtime never needs an API or network.
ENV LOCAL_EMBED_CACHE=/app/models \
    LOCAL_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/app/models').embed(['warmup']))"

# Copy application code (only this layer rebuilds when code changes)
ADD . /app
RUN rm -rf extra docs preview README.md LICENSE .gitignore

ENTRYPOINT ["/app/entrypoint.sh"]
