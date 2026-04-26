from fastapi import FastAPI
from src.db.base import Base
from src.db.session import engine
from src.api.routes import auth
from src.api.routes import repo

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(auth.router)
app.include_router(repo.router)