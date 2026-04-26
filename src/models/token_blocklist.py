from sqlalchemy import Column, String
from src.db.base import Base


class TokenBlocklist(Base):
    """Stores revoked JWT IDs (jti claim) to prevent reuse after logout."""

    __tablename__ = "token_blocklist"

    # Store only the jti (UUID4, 36 chars) — not the full JWT string
    jti = Column(String(36), primary_key=True, nullable=False)
