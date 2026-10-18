"""Wire contract shared by the CP server and remote worker client.

Domain errors travel as HTTP 200 with the CLI envelope; only transport-level
rejections (auth, routing, size, protocol) use non-200 statuses.
"""
from __future__ import annotations

import json

PROTOCOL_VERSION = 1
PROTOCOL_HEADER = 'X-CP-Protocol'
MAX_BODY = 2 * 1024 * 1024


def envelope_ok(result):
    return {'ok': True, 'result': result}


def envelope_fail(code, message):
    return {'ok': False, 'code': code, 'message': message}


def dumps(envelope):
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode('utf-8')


class ProtocolError(Exception):
    """Transport-level rejection mapped to an HTTP status + error code."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
