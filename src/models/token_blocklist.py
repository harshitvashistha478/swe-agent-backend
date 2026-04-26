from sqlalchemy import Column, String
from src.db.base import Base

class TokenBlocklist(Base):
    __tablename__ = "token_blocklist"
    token = Column(String, primary_key=True)