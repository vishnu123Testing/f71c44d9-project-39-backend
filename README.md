# Backend Service

Python 3.14.8 FastAPI API and Celery worker service for PairFront Virtual FDE.

## Setup & Installation

```bash
uv sync
```

## Running

```bash
uv run uvicorn src.api.main:app --reload
```

## Testing & Linting

```bash
uv run pytest
ruff check . && ruff format --check .
```
