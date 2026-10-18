"""Minimal RFC 6455 (WebSocket) framing for the command channel.

Vendored on purpose: the repo is standard-library only, and the channel needs
a deliberately small subset — small JSON text frames, server-initiated ping,
client pong/close. No fragmentation, no binary, no extensions: a frame that
uses any of that is a protocol failure (close 1002/1003/1009), which degrades
the agent to its reconnect loop and the command to its ledger wait — a
transport bug can never corrupt coordination state.
"""
import base64
import hashlib
import struct

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_FRAME = 64 * 1024

OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA
_RESERVED = (0x0, 0x3, 0x4, 0x5, 0x6, 0x7, 0xB, 0xC, 0xD, 0xE, 0xF)


class WsProtocolError(Exception):
    """A frame we refuse; ``code`` is the RFC 6455 close code to answer with."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def accept_key(client_key):
    """RFC 6455 §1.3: base64(SHA-1(key + GUID)) — the handshake's only math."""
    digest = hashlib.sha1((client_key + GUID).encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii')


def handshake_response(client_key):
    return ('HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            'Sec-WebSocket-Accept: %s\r\n\r\n' % accept_key(client_key)).encode('ascii')


def _encode(opcode, payload, mask):
    import os
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n if mask else n)
    elif n < 65536:
        header.append(0x80 | 126 if mask else 126)
        header += struct.pack('>H', n)
    else:
        header.append(0x80 | 127 if mask else 127)
        header += struct.pack('>Q', n)
    if not mask:
        return bytes(header) + payload
    mask_key = os.urandom(4)
    masked = bytearray(payload)
    for i in range(len(masked)):
        masked[i] ^= mask_key[i % 4]
    return bytes(header) + mask_key + bytes(masked)


def encode_frame(opcode, payload=b''):
    """Server→client frame: unmasked (only clients mask), FIN set, no
    fragmentation — every payload we send fits one frame."""
    return _encode(opcode, payload, mask=False)


def encode_client_frame(opcode, payload=b''):
    """Client→server frame: RFC 6455 §5.3 requires every client frame to be
    masked with a fresh random key."""
    return _encode(opcode, payload, mask=True)


def encode_client_text(text):
    return encode_client_frame(OP_TEXT, text.encode('utf-8'))


def encode_text(text):
    return encode_frame(OP_TEXT, text.encode('utf-8'))


def encode_ping():
    return encode_frame(OP_PING)


def encode_pong(payload=b''):
    return encode_frame(OP_PONG, payload)


def encode_close(code=1000):
    return encode_frame(OP_CLOSE, struct.pack('>H', code))


class FrameReader:
    """recv-exact adapter with an optional pre-read prefix: sockets expose
    recv, tests expose bytes, and a caller that already consumed bytes while
    reading the handshake response hands them over as ``initial`` — a frame
    the peer sent immediately after the 101 must never be dropped."""

    def __init__(self, source, initial=b''):
        self.source = source
        self.buf = bytearray(initial)

    def exact(self, n):
        while len(self.buf) < n:
            chunk = self.source.recv(n - len(self.buf)) if hasattr(self.source, 'recv')                 else self.source.read(n - len(self.buf))
            if not chunk:
                raise ConnectionError('connection closed mid-frame')
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def read_frame(source, masked_required=True, max_frame=MAX_FRAME):
    """Read one frame. A client endpoint (our server reading agent frames)
    requires the mask bit (RFC 6455 §5.3); a client endpoint reading our
    server frames passes ``masked_required=False`` (server frames are never
    masked). The channel accepts only single unfragmented text/control frames
    within ``max_frame`` — everything else raises WsProtocolError with the
    close code the reader should answer."""
    reader = source if isinstance(source, FrameReader) else FrameReader(source)
    b1, b2 = reader.exact(2)
    fin, opcode = b1 & 0x80, b1 & 0x0F
    rsv, masked, length = b1 & 0x70, b2 & 0x80, b2 & 0x7F
    if not fin or opcode in _RESERVED:
        raise WsProtocolError(1002, 'fragmented or reserved frames unsupported')
    if rsv:
        raise WsProtocolError(1002, 'RSV bits must be zero (no extensions negotiated)')
    if opcode >= 0x8 and length > 125:
        raise WsProtocolError(1002, 'control frames must carry at most 125 bytes')
    if opcode == OP_BINARY:
        raise WsProtocolError(1003, 'binary frames unsupported')
    if length == 126:
        length = struct.unpack('>H', reader.exact(2))[0]
    elif length == 127:
        length = struct.unpack('>Q', reader.exact(8))[0]
    if length > max_frame:
        raise WsProtocolError(1009, 'frame exceeds %d bytes' % max_frame)
    if bool(masked) != bool(masked_required):
        raise WsProtocolError(1002, 'frame mask does not match the endpoint')
    mask = reader.exact(4) if masked else None
    payload = bytearray(reader.exact(length))
    if mask:
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]
    return opcode, bytes(payload)
