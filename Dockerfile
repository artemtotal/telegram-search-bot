FROM python:3.9-slim
WORKDIR /app

# Install system dependencies (cached unless this changes)
RUN apt update && apt install gcc -y && apt clean

# Install Python dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN /usr/local/bin/python -m pip install --upgrade pip && pip install -r requirements.txt

# Copy application code (only this layer rebuilds when code changes)
ADD . /app
RUN rm -rf extra doc preview README.md LICENSE .gitignore

ENTRYPOINT ["/app/entrypoint.sh"]
