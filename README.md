# 🚀 RepoMind Backend — Running Services

This guide explains how to run the core services required for the backend:

* Redis (message broker)
* Celery workers (background jobs)
* FastAPI server (API layer)

---

## 🧱 Prerequisites

Make sure you have:

* Docker installed (for Redis)
* Python environment activated
* Dependencies installed (`pip install -r requirements.txt`)

---

## ▶️ Start Services

### 1️⃣ Start Redis (Broker)

```bash
docker run -d -p 6379:6379 redis
```

---

### 2️⃣ Start Celery Worker

```bash
celery -A src.worker.celery_app worker --loglevel=info
```

> ⚠️ If you're on Windows, use:

```bash
celery -A src.worker.celery_app worker --pool=solo --loglevel=info
```

---

### 3️⃣ Run FastAPI Server

```bash
uvicorn src.main:app --reload
```

---

## ⚡ Running Multiple Workers

To scale background processing:

```bash
celery -A src.worker.celery_app worker --loglevel=info --concurrency=4
```

* `--concurrency=4` → runs 4 worker processes
* Adjust based on your CPU cores

---

## 🧠 Notes

* Redis must be running before starting Celery
* Celery workers must be running for background jobs (repo import, etc.)
* FastAPI handles API requests, Celery handles heavy tasks

---

## 🛠️ Common Issues

### ❌ Tasks not executing

* Ensure Redis is running
* Ensure Celery worker is active

### ❌ Connection refused (Redis)

* Check if port `6379` is available
* Restart container if needed

### ❌ Windows multiprocessing issues

* Use `--pool=solo`

---

## 🔜 Next Steps

* Add monitoring (Flower for Celery)
* Add Docker Compose for full stack setup
* Integrate ingestion pipeline after repo cloning

---
