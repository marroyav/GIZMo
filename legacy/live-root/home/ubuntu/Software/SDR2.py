import mmap
import os
import numpy as np
import socket
import time
import select

BRAM_BASE_ADDR = 0xA0042000
BRAM_SIZE = 2048 * 4
NUM_SAMPLES = 2048
SEND_INTERVAL = 0.005  # seconds
TCP_PORT = 5556

def mmap_bram():
    mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    bram = mmap.mmap(
        fileno=mem_fd,
        length=BRAM_SIZE,
        flags=mmap.MAP_SHARED,
        prot=mmap.PROT_READ,
        offset=BRAM_BASE_ADDR
    )
    os.close(mem_fd)
    return bram

def read_bram(bram):
    bram.seek(0)
    return np.frombuffer(bram.read(BRAM_SIZE), dtype=np.uint32).astype(np.int32)

def main():
    # Set up TCP server
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", TCP_PORT))
    server_socket.listen()
    server_socket.setblocking(False)
    print(f"TCP server listening on port {TCP_PORT}...")

    clients = []

    bram = mmap_bram()

    try:
        while True:
            # Accept new clients without blocking
            try:
                conn, addr = server_socket.accept()
                conn.setblocking(False)
                clients.append(conn)
                print(f"New client connected: {addr}")
            except BlockingIOError:
                pass  # No new connections

            # Read BRAM data
            data = read_bram(bram)
            raw_bytes = data.tobytes()

            # Broadcast to all clients
            for client in clients[:]:
                try:
                    # Use send() instead of sendall() to avoid blocking
                    sent = client.send(raw_bytes)
                    # If partial frame sent, ignore and try again next iteration
                    # Slow clients will automatically drop frames
                except (BlockingIOError, BrokenPipeError, ConnectionResetError):
                    clients.remove(client)
                    print("Client disconnected or too slow.")

            time.sleep(SEND_INTERVAL)

    except KeyboardInterrupt:
        print("Shutting down server.")
    finally:
        bram.close()
        for client in clients:
            client.close()
        server_socket.close()

if __name__ == "__main__":
    main()
