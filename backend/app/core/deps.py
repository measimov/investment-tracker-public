import secrets
from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.user import User
from ..core.security import AUTH_COOKIE_NAME, CSRF_COOKIE_NAME, decode_access_token

# HTTP Bearer token scheme
security = HTTPBearer(auto_error=False)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Get the current authenticated user from a Bearer token or secure browser cookie.

    Args:
        request: Current request, including cookies and CSRF header
        credentials: Optional HTTP Bearer credentials from request header
        db: Database session

    Returns:
        The authenticated User object

    Raises:
        HTTPException: If token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    bearer_token = credentials.credentials if credentials else None
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    token = bearer_token or cookie_token
    if not token:
        raise credentials_exception

    if not bearer_token and request.method.upper() not in SAFE_METHODS:
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if (
            not csrf_cookie
            or not csrf_header
            or not secrets.compare_digest(
                csrf_cookie,
                csrf_header,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            )

    # Decode token
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    username: Optional[str] = payload.get("sub")
    jti: Optional[str] = payload.get("jti")
    if username is None or jti is None:
        raise credentials_exception

    # Get user from database
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception

    # A JWT is only accepted while its server-side session is live, so logout,
    # password changes and admin actions can actually revoke it (issue #36).
    from ..services.auth_session_service import is_session_valid

    if not is_session_valid(db, jti, user.id):
        raise credentials_exception

    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """
    Ensure the current user is active.

    Args:
        current_user: The current authenticated user

    Returns:
        The current user if active

    Raises:
        HTTPException: If user is inactive
    """
    if not current_user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user")
    return current_user


def get_current_admin_user(current_user: User = Depends(get_current_active_user)) -> User:
    """
    Ensure the current user is an admin.

    Args:
        current_user: The current active user

    Returns:
        The current user if they are an admin

    Raises:
        HTTPException: If user is not an admin
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
    return current_user
