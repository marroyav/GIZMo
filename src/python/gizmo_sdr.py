#!/usr/bin/env python3
"""Stream fixed-size ADC BRAM frames over TCP without corrupting partial sends."""

from __future__ import annotations

import mmap
import os
import socket
import time

import numpy as np


BRAM_BASE_ADDRESS = int(os.environ.get("GIZMO_ADC_BRAM_ADDRESS", "0xA0042000"), 0)
SAMPLE_COUNT = int(os.environ.get("GIZMO_SDR_SAMPLE_COUNT", "2048"))
BRAM_SIZE = SAMPLE_COUNT * 4
SEND_INTERVAL = float(os.environ.get("GIZMO_SDR_SEND_INTERVAL", "0.005"))
HOST = os.environ.get("GIZMO_SDR_HOST", "0.0.0.0")
PORT = int(os.environ.get("GIZMO_SDR_PORT", "5556"))


def map_bram() -> mmap.mmap:
    memory_fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        return mmap.mmap(
            fileno=memory_fd,
            length=BRAM_SIZE,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ,
            offset=BRAM_BASE_ADDRESS,
        )
    finally:
        os.close(memory_fd)


def read_frame(bram: mmap.mmap) -> bytes:
    bram.seek(0)
    values = np.frombuffer(bram.read(BRAM_SIZE), dtype=np.uint32).astype(np.int32)
    return values.tobytes()


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    server.setblocking(False)
    clients: dict[socket.socket, tuple[bytes, int] | None] = {}
    bram = map_bram()
    print(f"SDR server listening on {HOST}:{PORT}", flush=True)

    try:
        while True:
            try:
                while True:
                    connection, address = server.accept()
                    connection.setblocking(False)
                    clients[connection] = None
                    print(f"SDR client connected: {address}", flush=True)
            except BlockingIOError:
                pass

            frame = read_frame(bram)
            for client in list(clients):
                pending = clients[client]
                if pending is None:
                    pending = (frame, 0)
                pending_frame, offset = pending
                try:
                    sent = client.send(pending_frame[offset:])
                    if sent == 0:
                        raise ConnectionResetError("zero-length send")
                    offset += sent
                    clients[client] = None if offset == len(pending_frame) else (pending_frame, offset)
                except BlockingIOError:
                    clients[client] = pending
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client.close()
                    del clients[client]
                    print("SDR client disconnected", flush=True)

            time.sleep(SEND_INTERVAL)
    finally:
        bram.close()
        for client in clients:
            client.close()
        server.close()


if __name__ == "__main__":
    main()
