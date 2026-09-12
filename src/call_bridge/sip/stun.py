from __future__ import annotations

import asyncio
import secrets
import socket
import struct

MAGIC = 0x2112A442
BINDING_REQUEST = 0x0001
XOR_MAPPED_ADDRESS = 0x0020
MAPPED_ADDRESS = 0x0001


async def discover_public_address(host: str, port: int, timeout: float = 2.0) -> tuple[str, int]:
    """RFC 5389 STUN Binding request. Used so SDP advertises a reachable IP."""
    txid = secrets.token_bytes(12)
    request = struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC) + txid
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, request, (host, port))
        data, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout)
    finally:
        sock.close()
    if len(data) < 20:
        raise RuntimeError("short STUN response")
    msg_type, length, magic = struct.unpack("!HHI", data[:8])
    if magic != MAGIC or data[8:20] != txid:
        raise RuntimeError("STUN response did not match transaction")
    offset = 20
    end = 20 + length
    while offset + 4 <= end and offset + 4 <= len(data):
        atype, alen = struct.unpack("!HH", data[offset : offset + 4])
        value = data[offset + 4 : offset + 4 + alen]
        offset += 4 + alen
        if offset % 4:
            offset += 4 - (offset % 4)
        if atype == XOR_MAPPED_ADDRESS and len(value) >= 8 and value[1] == 0x01:
            xport = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
            xip = struct.unpack("!I", value[4:8])[0] ^ MAGIC
            ip = socket.inet_ntoa(struct.pack("!I", xip))
            return ip, xport
        if atype == MAPPED_ADDRESS and len(value) >= 8 and value[1] == 0x01:
            port_n = struct.unpack("!H", value[2:4])[0]
            ip = socket.inet_ntoa(value[4:8])
            return ip, port_n
    raise RuntimeError("STUN response had no mapped address")
