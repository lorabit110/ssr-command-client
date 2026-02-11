"""
Adapted from [Xiangsong Zeng](https://gist.github.com/zengxs)
Reference gist: proxy_socks2http.py https://gist.github.com/zengxs/dc6cb4dea4495ecaab7b44abb07a581f
"""
import asyncio
import logging
import re
from asyncio import StreamReader, StreamWriter
from collections import namedtuple
from typing import Optional

import socks  # use pysocks

logging.basicConfig(level=logging.INFO)

# Timeout for SOCKS5 connect and idle data transfer (seconds)
CONNECT_TIMEOUT = 30
IO_TIMEOUT = 300

HttpHeader = namedtuple(
    "HttpHeader", ["method", "url", "version", "connect_to", "is_connect"]
)


def _close_writer(writer: StreamWriter):
    """Safely close a StreamWriter."""
    try:
        writer.close()
    except Exception:
        pass


async def _relay(reader: StreamReader, writer: StreamWriter):
    """Copy data from reader to writer until EOF or error, then close writer."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(8192), timeout=IO_TIMEOUT)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError):
        pass
    finally:
        _close_writer(writer)


async def _dial(client_conn, server_conn):
    """Establish bidirectional relay, wait for both directions to finish."""
    t1 = asyncio.ensure_future(_relay(client_conn[0], server_conn[1]))
    t2 = asyncio.ensure_future(_relay(server_conn[0], client_conn[1]))
    await asyncio.gather(t1, t2, return_exceptions=True)


async def open_socks5_connection(
    host: str,
    port: int,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    socks_host: str = "localhost",
    socks_port: int = 1080,
    limit=2**16,
):
    loop = asyncio.get_event_loop()
    s = socks.socksocket()
    s.settimeout(CONNECT_TIMEOUT)
    s.set_proxy(
        socks.SOCKS5,
        addr=socks_host,
        port=socks_port,
        username=username,
        password=password,
    )
    try:
        await loop.run_in_executor(None, s.connect, (host, port))
        s.setblocking(False)
        reader = StreamReader(limit=limit)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_connection(lambda: protocol, sock=s)
        writer = StreamWriter(transport, protocol, reader, loop)
        return reader, writer
    except Exception:
        s.close()
        raise


async def read_until_end_of_http_header(reader: StreamReader) -> bytes:
    lines = []
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)
        if not line:
            raise IOError("Client disconnected before sending full header")
        lines.append(line)
        if line == b"\r\n":
            break

    return b"".join(lines)


def parse_http_header(header: bytes) -> HttpHeader:
    lines = header.split(b"\r\n")
    fl = lines[0].decode()
    method, url, version = fl.split(" ", 2)

    if method.upper() == "CONNECT":
        host, port = url.split(":", 1)
        port = int(port)
    else:
        # find Host header line
        host_text = None
        for header_line in lines:
            hl = header_line.decode()
            if re.match(r"^host:", hl, re.IGNORECASE):
                host_text = re.sub(r"^host:\s*", "", hl, count=1, flags=re.IGNORECASE)
                break

        if not host_text:
            raise ValueError("No http host line")

        if ":" not in host_text:
            host = host_text
            port = 80
        else:
            host, port = host_text.split(":", 1)
            port = int(port)

    is_connect = method.upper() == "CONNECT"
    return HttpHeader(
        method=method,
        url=url,
        version=version,
        connect_to=(host, port),
        is_connect=is_connect,
    )


async def handle_connection(reader: StreamReader, writer: StreamWriter, socks_port=1080):
    server_conn = None
    try:
        http_header_bytes = await read_until_end_of_http_header(reader)
        http_header = parse_http_header(http_header_bytes)

        server_conn = await open_socks5_connection(
            host=http_header.connect_to[0],
            port=http_header.connect_to[1],
            socks_host="localhost",
            socks_port=socks_port,
        )

        if http_header.is_connect:
            writer.write(b"HTTP/1.0 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            server_conn[1].write(http_header_bytes)
            await server_conn[1].drain()

        await _dial((reader, writer), server_conn)

    except Exception as e:
        logging.debug("connection error: %s", e)
        # Clean up server side if it was opened
        if server_conn:
            _close_writer(server_conn[1])
        _close_writer(writer)


def main():
    loop = asyncio.get_event_loop()
    server = asyncio.start_server(handle_connection, host="localhost", port=7890)
    try:
        server = loop.run_until_complete(server)
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())


if __name__ == "__main__":
    main()
