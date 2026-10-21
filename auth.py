# auth.py  -  password hashing and login tokens (JWT).
#
# How login works:
#   1. User sends email + password to /api/login
#   2. We check the password against the hash saved in the users table
#   3. We send back a TOKEN (a signed piece of text)
#   4. React sends that token with every later request:
#         Authorization: Bearer <token>
#   5. get_current_user() reads the token and tells us who the user is

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()  # optional: put SECRET_KEY=... in a .env file
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret")
ALGORITHM = "HS256"

bearer_scheme = HTTPBearer()


def hash_password(plain_password):
    return bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password, hashed_password):
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except ValueError:  # the stored value is not a valid bcrypt hash
        return False


def create_token(user):
    payload = {
        "sub": str(user["user_id"]),
        "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=8),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Use as a dependency on any route that needs a logged-in user."""
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"user_id": int(payload["sub"]), "role": payload["role"]}


def require_admin(user=Depends(get_current_user)):
    """Use as a dependency on admin-only routes."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")
    return user


def require_seller(user=Depends(get_current_user)):
    """Use as a dependency on seller-only routes."""
    if user["role"] != "seller":
        raise HTTPException(status_code=403, detail="Seller access only")
    return user