from fastapi import Header
from jose import jwt
import security 

def get_optional_user(authorization: str = Header(None)):
    """
    Decodes the JWT if it exists. 
    Returns the user_id if valid, or None if guest/invalid.
    """
    if not authorization:
        return None

    try:
        # The header usually looks like "Bearer <token>"
        token = authorization.split(" ")[1]
        payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
        return payload.get("sub")
    except Exception:
        # If the token is expired or tampered with, treat them as a guest
        return None