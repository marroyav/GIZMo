#!/usr/bin/env python3
"""GIZMo ZeroMQ command server.

This preserves the legacy TCP/5555 protocol while replacing the broad legacy
startup-script restart with the package's narrow, Unix-socket control service.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import socket
import time

import zmq

from gizmo_common import CONTROL_SOCKET, atomic_write, read_exported_int, state_path


ZMQ_ENDPOINT = os.environ.get("GIZMO_ZMQ_ENDPOINT", "tcp://*:5555")
ZMON_HOST = os.environ.get("GIZMO_ZMON_HOST", "127.0.0.1")
ZMON_PORT = int(os.environ.get("GIZMO_ZMON_PORT", "5055"))
CONTROL_TIMEOUT = float(os.environ.get("GIZMO_CONTROL_TIMEOUT", "10"))
TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$")


def request_control(command: str) -> str:
    """Send one privileged, allow-listed request to gizmo-control."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(CONTROL_TIMEOUT)
        control.connect(CONTROL_SOCKET)
        control.sendall(f"{command}\n".encode("ascii"))
        response = control.recv(512).decode("utf-8", errors="replace").strip()
    if not response.startswith("OK "):
        raise RuntimeError(response or "empty response from gizmo-control")
    return response[3:]


def get_zmon_data() -> str:
    try:
        with socket.create_connection((ZMON_HOST, ZMON_PORT), timeout=3) as client:
            return client.recv(2048).decode("utf-8", errors="replace")
    except OSError as error:
        print(f"Unable to read ZMon on {ZMON_HOST}:{ZMON_PORT}: {error}", flush=True)
        return "Failed to get data from C-server"


def write_zmon_arguments(*arguments: str) -> None:
    padded = list(arguments[:3]) + [""] * (3 - len(arguments))
    for index, argument in enumerate(padded, start=1):
        if any(character in argument for character in ('"', "\n", "\r")):
            raise ValueError("invalid character in ZMon argument")
        atomic_write(state_path(f"ZMonArg{index}.env"), f'ZMonArg{index}="{argument}"\n')


def restart_zmon(*arguments: str) -> str:
    write_zmon_arguments(*arguments)
    return request_control("restart-zmon")


def parse_integer(message: str, command: str, *, allow_zero: bool) -> int:
    parts = message.split()
    if len(parts) != 2 or parts[0] != command or not parts[1].isdigit():
        raise ValueError(f"Invalid format. Use '{command} N' where N is a number.")
    value = int(parts[1])
    minimum = 0 if allow_zero else 1
    if value < minimum or value > 1_000_000:
        raise ValueError(f"{command} must be between {minimum} and 1000000.")
    return value


def read_text_file(name: str) -> str:
    try:
        return state_path(name).read_text(encoding="utf-8")
    except OSError as error:
        return f"Unable to read {name}: {error}"


def handle_message(message: str) -> str:
    run_interval = read_exported_int("setRunInterval.env", "runInterval", 100)
    threshold = read_exported_int("setThreshold.env", "threshold", 100)

    if message.startswith("run "):
        interval = parse_integer(message, "run", allow_zero=False)
        atomic_write(state_path("setRunInterval.env"), f"export runInterval={interval}\n")
        result = restart_zmon(f"set_th {threshold}", f"run {interval}")
        return f"Measurement interval updated to {interval}; {result}."

    if message.startswith("CAL "):
        interval = parse_integer(message, "CAL", allow_zero=False)
        atomic_write(state_path("setRunInterval.env"), f"export runInterval={interval}\n")
        result = restart_zmon(f"CAL {interval}", f"set_th {threshold}", f"run {interval}")
        return f"Calibration requested with {interval} reads; {result}."

    if message in ("get_data", "get_data_continuous"):
        return f"Data from C-server: {get_zmon_data()}"

    if message.startswith("set_th "):
        new_threshold = parse_integer(message, "set_th", allow_zero=True)
        atomic_write(state_path("setThreshold.env"), f"export threshold={new_threshold}\n")
        result = restart_zmon(f"set_th {new_threshold}", f"run {run_interval}")
        return f"Threshold updated to {new_threshold}; {result}."

    if message == "read_adc":
        result = restart_zmon("read_adc", f"set_th {threshold}", f"run {run_interval}")
        return f"ADC capture requested; {result}."

    if message.startswith("set_time "):
        requested = message.removeprefix("set_time ").strip()
        if not TIME_PATTERN.fullmatch(requested):
            return "Invalid time format. Use 'set_time YYYY-MM-DD HH:MM:SS[.SSSSSS]'."
        parsed = dt.datetime.strptime(requested.split(".", 1)[0], "%Y-%m-%d %H:%M:%S")
        epoch_seconds = int(time.mktime(parsed.timetuple()))
        time_result = request_control(f"set-time {epoch_seconds}")
        restart_result = request_control("restart-zmon")
        return f"{time_result}; {restart_result}."

    if message == "get_adc":
        return read_text_file("adc.csv")
    if message == "get_Rcal":
        return read_text_file("Rcalibration.csv")
    if message == "get_Ccal":
        return read_text_file("Ccalibration.csv")

    if message == "clear_latch":
        atomic_write(state_path("latchState.env"), "latched=0\n\n")
        return "Cleared Latch value in latchState.env"

    if message == "testHello.py":
        return "Legacy testHello.py helper is not part of gizmo-runtime"

    return "Unknown command"


def main() -> None:
    context = zmq.Context.instance()
    server = context.socket(zmq.REP)
    server.setsockopt(zmq.LINGER, 0)
    server.bind(ZMQ_ENDPOINT)
    print(f"GIZMo ZMQ server listening at {ZMQ_ENDPOINT}", flush=True)

    try:
        while True:
            message = server.recv_string()
            print(f"Received: {message}", flush=True)
            try:
                reply = handle_message(message)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Command failed: {error}", flush=True)
                reply = f"Command failed: {error}"
            server.send_string(reply)
    finally:
        server.close()


if __name__ == "__main__":
    main()
