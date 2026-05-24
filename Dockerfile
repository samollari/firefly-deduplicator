FROM astral/uv:python3.12-trixie-slim

WORKDIR /app

COPY .python-version pyproject.toml uv.lock ./

RUN uv sync --locked

COPY main.py .

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]