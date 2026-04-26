import logging

from jose import JWTError
from sqlalchemy.orm import Session

from src.core.security import create_access_token, decode_token
from src.models.token_blocklist import TokenBlocklist
from src.models.user import User
from src.utils.hashing import hash_password, verify_password

logger = logging.getLogger(__name__)


def register_user(db: Session, email: str, password: str) -> User:
    """Create a new user. Raises ValueError if email already taken."""
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise ValueError("Email already registered")
    user = User(email=email, password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("New user registered: id=%s", user.id)
    return user


def authenticate_user(db: Session, email: str, password: str):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password):
        return None
    return user


def login(db: Session, email: str, password: str):
    user = authenticate_user(db, email, password)
    if not user:
        return None
    token = create_access_token({"sub": str(user.id)})
    logger.info("User logged in: id=%s", user.id)
    return token


def logout(db: Session, token: str) -> None:
    """Revoke a JWT by storing its jti in the blocklist."""
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        if jti:
            db.add(TokenBlocklist(jti=jti))
            db.commit()
            logger.info("Token revoked: jti=%s", jti)
    except JWTError:
        logger.warning("Logout called with an invalid token")
