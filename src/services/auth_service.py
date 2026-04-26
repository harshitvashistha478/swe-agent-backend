from sqlalchemy.orm import Session
from src.models.user import User
from src.models.token_blocklist import TokenBlocklist
from src.utils.hashing import verify_password
from src.core.security import create_access_token


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
    return token


def logout(db: Session, token: str):
    db.add(TokenBlocklist(token=token))
    db.commit()