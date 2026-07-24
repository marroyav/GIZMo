#!/usr/bin/env python3
import smbus2
import socket
import threading
import time
import os

# MCP9808 I2C setup
MCP9808_ADDR = 0x18
REG_AMBIENT_TEMP = 0x05
I2C_BUS = 7

# CPU temperature file paths
CPU_TEMP_FILES = [
    "/sys/class/hwmon/hwmon0/temp1_input",
    "/sys/class/hwmon/hwmon0/temp2_input",
    "/sys/class/hwmon/hwmon0/temp3_input"
]

# Socket configuration
HOST = '0.0.0.0'
PORT = 5005

# Initialize I2C bus
try:
    bus = smbus2.SMBus(I2C_BUS)
except FileNotFoundError:
    print(f"Error: /dev/i2c-{I2C_BUS} not found.")
    exit(1)
except PermissionError:
    print(f"Error: Permission denied on /dev/i2c-{I2C_BUS}.")
    exit(1)

# Function to read MCP9808 temperature
def read_chassis_temp():
    raw = bus.read_i2c_block_data(MCP9808_ADDR, REG_AMBIENT_TEMP, 2)
    temp = (raw[0] << 8) | raw[1]
    temp &= 0x0FFF
    celsius = temp / 16.0
    if raw[0] & 0x10:  # negative temp flag
        celsius -= 256
    return celsius

# Function to read CPU temperatures
def read_cpu_temps():
    temps = []
    for file_path in CPU_TEMP_FILES:
        try:
            with open(file_path, "r") as f:
                # The value in hwmon files is in millidegree Celsius
                temp_mC = int(f.read().strip())
                temps.append(temp_mC / 1000.0)
        except Exception:
            temps.append(None)  # in case the file doesn't exist or can't be read
    return temps

# Function to handle each client
def handle_client(conn, addr):
    print(f"Client connected: {addr}")
    with conn:
        while True:
            try:
                chassis_temp = read_chassis_temp()
                cpu_temps = read_cpu_temps()
                message = (
                    f"Chassis={chassis_temp:.2f}, "
                    f"CPU1={cpu_temps[0]:.2f}, "
                    f"CPU2={cpu_temps[1]:.2f}, "
                    f"CPU3={cpu_temps[2]:.2f}\n"
                ).encode('utf-8')
                conn.sendall(message)
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                print(f"Client disconnected: {addr}")
                break
            except Exception as e:
                print(f"Error reading sensors: {e}")
                time.sleep(1)

# Main server loop
def run_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.listen()
        print(f"Temperature socket server listening on {HOST}:{PORT}")

        while True:
            try:
                conn, addr = s.accept()
                threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
            except KeyboardInterrupt:
                print("Server shutting down...")
                break

if __name__ == "__main__":
    run_server()
