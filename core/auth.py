import jwt
import bcrypt
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

load_dotenv()

SECRET_KEY = os.getenv("NEXUS_SECRET_KEY", secrets.token_hex(32))
TOKEN_EXPIRY_HOURS = 24

VALID_USERNAME = os.getenv("NEXUS_USERNAME", "nexus")
HASHED_PASSWORD = bcrypt.hashpw(
    os.getenv("NEXUS_PASSWORD", "nexus").encode(),
    bcrypt.gensalt()
)


def verify_credentials(username: str, password: str) -> bool:
    if username != VALID_USERNAME:
        return False
    return bcrypt.checkpw(password.encode(), HASHED_PASSWORD)


def create_token() -> str:
    payload = {"exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_token(token: str) -> bool:
    try:
        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return True
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return False
