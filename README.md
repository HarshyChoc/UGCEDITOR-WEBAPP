# ReclipUGCEditor

## Web app (FastAPI + RQ)

This repo now includes a web version that reuses the existing `processor.py` and `ugc_processor.py` logic.

### Local dev

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Start Redis (local or Docker):

```bash
docker run --rm -p 6379:6379 redis:7
```

3. Start the worker:

```bash
python -m webapp.worker
```

4. Start the web server:

```bash
uvicorn webapp.main:app --reload
```

5. Open `http://localhost:8000`.

### Environment variables

- `ASSEMBLYAI_API_KEY`: required if captions are enabled for UGC.
- `REDIS_URL`: Redis connection string (default `redis://localhost:6379/0`).
- `RECLIP_DATA_DIR`: where uploads and outputs are stored (default `./data`).

### Docker

Use the included `docker-compose.yml` to run web + worker + Redis together.

```bash
docker compose up --build
```
# UGCEDITOR-WEBAPP
