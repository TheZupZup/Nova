import httpx
from urllib.parse import urlencode

from config import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_OAUTH_REDIRECT_URI,
    NOVA_ALPHA_ALLOWED_USERS,
)

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"


def build_auth_url(state: str) -> str:
    return _AUTHORIZE_URL + "?" + urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_OAUTH_REDIRECT_URI,
        "scope": "read:user",
        "state": state,
    })


async def exchange_code(code: str) -> str | None:
    """Exchange an OAuth code for an access token. Returns the token or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": GITHUB_OAUTH_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token") or None
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_username(token: str) -> str | None:
    """Return the GitHub login for the given access token, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _USER_URL,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.status_code != 200:
            return None
        return resp.json().get("login") or None
    except (httpx.HTTPError, ValueError):
        return None


def is_allowed(username: str) -> bool:
    return username.lower() in NOVA_ALPHA_ALLOWED_USERS
