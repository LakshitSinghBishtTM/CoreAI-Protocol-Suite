"""Small compatibility surface for the legacy ``python-jose`` imports.

The project uses PyJWT for signing and verification. Keeping this module lets
downstream callers that import ``jose.jwt`` continue to work without pulling
in the unmaintained python-jose dependency.
"""

import jwt
from jwt import DecodeError, ExpiredSignatureError, InvalidTokenError

JWTError = InvalidTokenError

__all__ = ["DecodeError", "ExpiredSignatureError", "JWTError", "jwt"]
