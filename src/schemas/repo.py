from pydantic import BaseModel, HttpUrl
from typing import Optional, Literal

class RepoImportRequest(BaseModel):
    repo_url: HttpUrl
    visibility: Literal["public", "private"]
    access_token: Optional[str] = None

class RepoImportResponse(BaseModel):
    message: str
    repo_name: str