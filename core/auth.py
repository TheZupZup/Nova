import jwt
import bcrypt
import logging
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

_secret_key_env = os.getenv("NOVA_SECRET_KEY")
if _secret_key_env is None:
    logger.warning(
        "NOVA_SECRET_KEY is not set; using a temporary secret key. "
        "Sessions will be invalidated on restart."
    )
    SECRET_KEY = secrets.token_hex(32)
else:
    SECRET_KEY = _secret_key_env
TOKEN_EXPIRY_HOURS = 24


VALID_USERNAME = os.getenv("NOVA_USERNAME", "nova")
HASHED_PASSWORD = bcrypt.hashpw(
    os.getenv("NOVA_PASSWORD", "nova").encode(),
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
