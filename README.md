# RepoMind

RepoMind clones a Git repository, builds a Neo4j knowledge graph of its code, then uses a local LLM (Ollama) to analyse every function — bottom-up through the call graph — for security, performance, and quality issues.

**Stack:** FastAPI · Celery · PostgreSQL · Redis · Neo4j · React · Ollama

---

## Prerequisites

- Python 3.12+, Node 18+, Docker Desktop, Git
- PostgreSQL running locally via pgAdmin (`swe_agent` database created)
- [Ollama](https://ollama.com) installed and running

---

## 1 — Environment

```bash
cp .env.example .env
```

Fill in `.env`:

```env
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/swe_agent
SECRET_KEY=<run: openssl rand -hex 64>
NEO4J_PASSWORD=repomind_graph   # must match docker-compose.yml
REPOS_BASE_PATH=C:/repomind-repos
```

Everything else can stay as the defaults in `.env.example`.

---

## 2 — Ollama models

```bash
ollama pull qwen2.5-coder:0.5b   # file descriptions
ollama pull nomic-embed-text      # embeddings
ollama pull llama3.1:8b           # analysis + chat
```

---

## 3 — Start Redis and Neo4j

```bash
docker compose up -d redis neo4j
```

Neo4j takes ~60 seconds on first boot. Watch with `docker compose logs -f neo4j`.

---

## 4 — Backend

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
# source .venv/bin/activate                       # Mac/Linux

pip install -r requirements.txt
alembic upgrade head
uvicorn src.main:app --reload
```

---

## 5 — Celery worker

Open a second terminal:

```bash
.venv\Scripts\activate
celery -A src.worker.celery_app worker --loglevel=info

# Windows (if you hit multiprocessing errors)
celery -A src.worker.celery_app worker --pool=solo --loglevel=info
```

---

## 6 — Frontend

```bash
cd ../frontend
npm install
npm run dev
```

App at **http://localhost:5173** · API docs at **http://localhost:8000/api/docs** · Neo4j at **http://localhost:7474**
