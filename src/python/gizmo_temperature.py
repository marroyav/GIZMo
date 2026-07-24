#!/usr/bin/env python3
"""TCP temperature service for the chassis sensor and Kria CPU sensors."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import smbus2


MCP9808_ADDRESS = int(os.environ.get("GIZMO_MCP9808_ADDRESS", "0x18"), 0)
AMBIENT_TEMPERATURE_REGISTER = 0x05
I2C_BUS = int(os.environ.get("GIZMO_I2C_BUS", "7"))
HOST = os.environ.get("GIZMO_TEMPERATURE_HOST", "0.0.0.0")
PORT = int(os.environ.get("GIZMO_TEMPERATURE_PORT", "5005"))
CPU_TEMPERATURE_FILES = tuple(
    Path(item)
    for item in os.environ.get(
        "GIZMO_CPU_TEMPERATURE_FILES",
        "/sys/class/hwmon/hwmon0/temp1_input:"
        "/sys/class/hwmon/hwmon0/temp2_input:"
        "/sys/class/hwmon/hwmon0/temp3_input",
    ).split(":")
)


def read_chassis_temperature(bus: smbus2.SMBus) -> float:
    raw = bus.read_i2c_block_data(MCP9808_ADDRESS, AMBIENT_TEMPERATURE_REGISTER, 2)
    value = ((raw[0] << 8) | raw[1]) & 0x0FFF
    celsius = value / 16.0
    if raw[0] & 0x10:
        celsius -= 256
    return celsius


def read_cpu_temperatures() -> list[float]:
    temperatures: list[float] = []
    for path in CPU_TEMPERATURE_FILES:
        try:
            temperatures.append(int(path.read_text(encoding="ascii").strip()) / 1000.0)
        except (OSError, ValueError):
            temperatures.append(float("nan"))
    while len(temperatures) < 3:
        temperatures.append(float("nan"))
    return temperatures


def handle_client(
    connection: socket.socket,
    address: tuple[str, int],
    bus: smbus2.SMBus,
    bus_lock: threading.Lock,
) -> None:
    print(f"Temperature client connected: {address}", flush=True)
    with connection:
        while True:
            try:
                with bus_lock:
                    chassis = read_chassis_temperature(bus)
                cpu = read_cpu_temperatures()
                message = (
                    f"Chassis={chassis:.2f}, CPU1={cpu[0]:.2f}, "
                    f"CPU2={cpu[1]:.2f}, CPU3={cpu[2]:.2f}\n"
                )
                connection.sendall(message.encode("utf-8"))
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                break
            except OSError as error:
                print(f"Temperature read failed for {address}: {error}", flush=True)
                time.sleep(1)
    print(f"Temperature client disconnected: {address}", flush=True)


def main() -> None:
    try:
        bus = smbus2.SMBus(I2C_BUS)
    except OSError as error:
        raise SystemExit(f"Unable to open /dev/i2c-{I2C_BUS}: {error}") from error

    bus_lock = threading.Lock()
    with bus, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen()
        print(f"Temperature server listening on {HOST}:{PORT}", flush=True)
        while True:
            connection, address = server.accept()
            thread = threading.Thread(
                target=handle_client,
                args=(connection, address, bus, bus_lock),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    main()
