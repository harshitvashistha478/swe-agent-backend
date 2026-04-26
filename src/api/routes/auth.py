import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user, get_db, oauth2_scheme
from src.core.limiter import limiter
from src.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from src.services.auth_service import login, logout, register_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new account and return an access token."""
    try:
        register_user(db, req.email, req.password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    token = login(db, req.email, req.password)
    return {"access_token": token}


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login_user(request: Request, req: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate and receive a JWT. Rate-limited to 5 attempts per minute per IP."""
    token = login(db, req.email, req.password)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": token}


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Revoke the current access token."""
    logout(db, token)
    return {"message": "Logged out successfully"}


@router.get("/me")
def get_me(user_id: str = Depends(get_current_user)):
    return {"user_id": user_id}
