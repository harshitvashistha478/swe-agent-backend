from pydantic import BaseModel, HttpUrl
from typing import Optional, Literal, List


class RepoImportRequest(BaseModel):
    repo_url: HttpUrl
    visibility: Literal["public", "private"]
    access_token: Optional[str] = None


class RepoImportResponse(BaseModel):
    message: str
    repo_name: str


class RepoListItem(BaseModel):
    task_id: str
    repo_name: str
    status: str


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    repo_name: str                      # e.g. "owner/repo"
    message: str
    history: List[ChatMessage] = []     # prior turns for multi-turn context


class ChatResponse(BaseModel):
    answer: str
