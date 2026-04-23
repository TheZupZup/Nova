import jwt
import bcrypt
from datetime import datetime, timedelta

# Change ces valeurs avant de mettre en production!
SECRET_KEY = "nexus-secret-change-moi"
TOKEN_EXPIRY_HOURS = 24

# Mot de passe par défaut : "nexus"
# Pour générer un nouveau hash :
# python -c "import bcrypt; print(bcrypt.hashpw(b'tonmotdepasse', bcrypt.gensalt()).decode())"
HASHED_PASSWORD = bcrypt.hashpw(b"w*5zbVDZ4Naw90MWC7gG", bcrypt.gensalt())


def verify_password(plain_password: str) -> bool:
    """Vérifie si le mot de passe est correct."""
    return bcrypt.checkpw(plain_password.encode(), HASHED_PASSWORD)


def create_token() -> str:
    """Génère un token JWT valide pour 24h."""
    payload = {
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_token(token: str) -> bool:
    """Vérifie si le token JWT est valide et non expiré."""
    try:
        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return True
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False
