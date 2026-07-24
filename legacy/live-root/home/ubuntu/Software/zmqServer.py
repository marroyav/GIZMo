#!/usr/bin/env python3
import zmq
import subprocess
import socket
import os
import time
import threading
import re

# Create the context and the socket for the server
context = zmq.Context()

# Socket to listen for incoming requests on port 5555
socket_5555 = context.socket(zmq.REP)
socket_5555.bind("tcp://*:5555")

# TCP settings for the C-script server
TCP_PORT = 5055
TCP_SERVER = "localhost"
BUFFER_SIZE = 2048

print("Server is listening on port 5555 and grabbing data from C-script's TCP server...")

def get_data_from_tcp_server():
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        tcp_socket.connect((TCP_SERVER, TCP_PORT))
        data = tcp_socket.recv(BUFFER_SIZE).decode('utf-8')
        return data
    except Exception as e:
        print(f"Error connecting to C-server on port {TCP_PORT}: {e}")
        return "Failed to get data from C-server"
    finally:
        tcp_socket.close()

while True:
    message = socket_5555.recv_string()
    print(f"Received: {message}")
    
    # Read the current value in setRunInterval.env
    try:
        with open("/home/ubuntu/Software/setRunInterval.env", "r") as file:
            for line in file:
                if line.startswith("export runInterval="):
                    value = line.strip().split('=')[1]
                    runInterval = int(value)
                else:
                    print("export runInterval not found in file.")
    except FileNotFoundError:
        print(f"Error: File '{file}' not found.")
    except ValueError as ve:
        print(f"Error reading setRunInterval.env: {ve}")
    except Exception as e:
        print(f"Unexpected error: {e}")

    # Read the current value in setThreshold.env
    try:
        with open("/home/ubuntu/Software/setThreshold.env", "r") as file:
            for line in file:
                if line.startswith("export threshold="):
                    value = line.strip().split('=')[1]
                    threshold = int(value)
                    break
                else:
                    print("export threshold not found in file.")
    except FileNotFoundError:
        print(f"Error: File '{file}' not found.")
    except ValueError as ve:
        print(f"Error reading setThreshold.env: {ve}")
    except Exception as e:
        print(f"Unexpected error: {e}")

    # Execute a test script
    if message == "testHello.py":
        try:
            result = subprocess.run(
                ["/home/ubuntu/Software/ZMQ/hello.py"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                reply = f"Script executed successfully:\n{result.stdout}"
            else:
                reply = f"Script error (code {result.returncode}):\n{result.stderr}"
        except Exception as e:
            reply = f"Execution failed: {e}"

    # Execute the run command and handle the number of seconds
    elif message.startswith("run "):
        try:
            parts = message.split()
            if len(parts) != 2 or not parts[1].isdigit():
                reply = "Invalid format. Use 'run N' where N is a number."
            else:
                runInterval = parts[1]

                # Update the interval in setRunInterval.env
                try:
                    with open("/home/ubuntu/Software/setRunInterval.env", "w") as f:
                        f.write(f"export runInterval={runInterval}\n")
                except Exception as e:
                    reply = f"Failed to write runInterval.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Write to ZMonArg1.env
                try:
                    with open("/home/ubuntu/Software/ZMonArg1.env", "w") as f:
                        f.write(f'ZMonArg1="set_th {threshold}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg1.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Write to ZMonArg2.env
                try:
                    with open("/home/ubuntu/Software/ZMonArg2.env", "w") as f:
                        f.write(f'ZMonArg2="run {runInterval}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg2.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Run rc.local in the background
                try:
                    subprocess.Popen(
                        ["sudo", "/etc/rc.local"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    reply = f"ZMonArg1.env updated to set_th {threshold}, ZMonArg2.env updated to run {runInterval}.\nScript launched in background."
                except Exception as e:
                    reply = f"ZMonArg1.env and ZMoneArg2.env updated, but failed to launch script: {e}"
                
        except Exception as e:
            reply = f"Execution failed: {e}"

    # Execute the CAL command and handle the number of seconds
    elif message.startswith("CAL "):
        try:
            parts = message.split()
            if len(parts) != 2 or not parts[1].isdigit():
                reply = "Invalid format. Use 'CAL N' where N is a number."
            else:
                runInterval = parts[1]

                # Update runInterval.env file with the number of seconds
                try:
                    with open("/home/ubuntu/Software/setRunInterval.env", "w") as f:
                        f.write(f"export runInterval={runInterval}\n")
                except Exception as e:
                    reply = f"Failed to write setRunInterval.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Update ZMonArg1 to CAL N
                try:
                    with open("/home/ubuntu/Software/ZMonArg1.env", "w") as f:
                        f.write(f'ZMonArg1="CAL {runInterval}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg1.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Update ZMonArg2 to set_th X
                try:
                    with open("/home/ubuntu/Software/ZMonArg2.env", "w") as f:
                        f.write(f'ZMonArg2="set_th {threshold}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg2.env: {e}"
                    socket_5555.send_string(reply)
                    continue
                
                # Update ZMonArg3 to run N
                try:
                    with open("/home/ubuntu/Software/ZMonArg3.env", "w") as f:
                        f.write(f'ZMonArg3="run {runInterval}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg3.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                try:
                    subprocess.Popen(
                        ["sudo", "/etc/rc.local"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    reply = f"ZMonArg1.env updated to CAL {runInterval}, ZMonArg2.env updated to set_th {threshold}, ZMonArg3.env updated to run {runInterval}.\nScript launched in background."

                except Exception as e:
                    reply = f"ZMonArg1.env updated, but failed to launch script: {e}"

        except Exception as e:
            reply = f"Execution failed: {e}"

    # Handles get_data request from client
    elif message == "get_data":
        c_script_response = get_data_from_tcp_server()
        reply = f"Data from C-server: {c_script_response}"

    # Handles a continuous reguest from the client (client just sends command repeatedly)
    elif message == "get_data_continuous":
        c_script_response = get_data_from_tcp_server()
        reply = f"Data from C-server: {c_script_response}"
    
    # Handles set_th X command and adjusts the value in setThreshold.env accordingly
    elif message.startswith("set_th "):
        try:
            parts = message.split()
            if len(parts) != 2 or not parts[1].isdigit():
                reply = "Invalid format. Use 'set_th N' where N is a number."
            else:
                threshold = parts[1]

                # Write to setThreshold.env
                try:
                    with open("/home/ubuntu/Software/setThreshold.env", "w") as f:
                        f.write(f"export threshold={threshold}\n")
                except Exception as e:
                    reply = f"Failed to write threshold.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Write to ZMonArg1.env
                try:
                    with open("/home/ubuntu/Software/ZMonArg1.env", "w") as f:
                        f.write(f'ZMonArg1="set_th {threshold}"\n')
                except Exception as e:
                    reply = f"Failed to write ZMonArg1.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Write to ZMonArg2.env
                try:
                    with open("/home/ubuntu/Software/ZMonArg2.env", "w") as f:
                        f.write(f'ZMonArg2="run {runInterval}"')
                except Exception as e:
                    reply = f"Failed to write ZMonArg2.env: {e}"
                    socket_5555.send_string(reply)
                    continue

                # Run rc.local in the background
                try:
                    subprocess.Popen(
                        ["sudo", "/etc/rc.local"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    reply = f"setThreshold.env updated to {threshold}, ZMonArg1 updated to set_th {threshold}, ZMonArg2 updated to run {runInterval}.\nScript launched in background."
                except Exception as e:
                    reply = f"ZMonArg1.env updated, but failed to launch script: {e}"
                
        except Exception as e:
            reply = f"Execution failed: {e}"

    # Handle command to read_adc on the control board
    elif message.startswith("read_adc"):
        # Update the interval in setRunInterval.env
        try:
            with open("/home/ubuntu/Software/setRunInterval.env", "w") as f:
                f.write(f"export runInterval={runInterval}\n")
        except Exception as e:
            reply = f"Failed to write runInterval.env: {e}"
            socket_5555.send_string(reply)
            continue
        
        # Write to setThreshold.env
        try:
            with open("/home/ubuntu/Software/setThreshold.env", "w") as f:
                f.write(f"export threshold={threshold}\n")
        except Exception as e:
            reply = f"Failed to write threshold.env: {e}"
            socket_5555.send_string(reply)
            continue

        # Write to ZMonArg1.env
        try:
            with open("/home/ubuntu/Software/ZMonArg1.env", "w") as f:
                f.write(f'ZMonArg1="read_adc"\n')
        except Exception as e:
            reply = f"Failed to write ZMonArg1.env: {e}"
            socket_5555.send_string(reply)
            continue
            

        # Write to ZMonArg2.env
        try:
            with open("/home/ubuntu/Software/ZMonArg2.env", "w") as f:
                f.write(f'ZMonArg2="set_th {threshold}"')
        except Exception as e:
            reply = f"Failed to write ZMonArg2.env: {e}"
            socket_5555.send_string(reply)
            continue
        
        # Write to ZMonArg3.env
        try:
            with open("/home/ubuntu/Software/ZMonArg3.env", "w") as f:
                f.write(f'ZMonArg3="run {runInterval}"')
        except Exception as e:
            reply = f"Failed to write ZMonArg2.env: {e}"
            socket_5555.send_string(reply)
            continue

        # Run rc.local in the background
        try:
            subprocess.Popen(
                ["sudo", "/etc/rc.local"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            reply = f"adc_read command sent to Binary for parsing, check adc.csv for output, ZMonArg1 updated to read_adc, ZMonArg2 updated to set_th {threshold}, ZMonArg3 updated to run {runInterval}.\nScript launched in background."
        except Exception as e:
            reply = f"ZMonArg1.env updated, but failed to launch script: {e}"
                
        except Exception as e:
            reply = f"Execution failed: {e}"
       
    # Handle command to set the system time
    elif message.startswith("set_time"):
        try:
            systemTime = message[len("set_time "):].strip()
            time_pattern = r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$'
            if not re.match(time_pattern, systemTime):
                reply = "Invalid time format. Use 'set_time T' where T is in 'YYYY-MM-DD HH:MM:SS[.SSSSSS]' format."
            else:
                date_command = ["sudo", "date", "-s", systemTime]


                try:
                    subprocess.run(date_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True
                    )
                    print("System time updated successfully.")
                    reply = "System Time has been updated"
                except subprocess.CalledProcessError as e:
                    print(f"Error updating system time: {e}")
                    reply = f"Error occured while setting the time: {e}"

                try:
                    subprocess.Popen(
                        ["sudo", "/etc/rc.local"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    print("Restarted rc.local")
                except Exception as e:
                    print(f"An error occurred while re-starting /etc/rc.local: {e}")

        except Exception as e:
                    reply = f"Error occured: {e}"

    elif message == "get_adc":
        with open("/home/ubuntu/Software/adc.csv", "r") as f:
            csv_str = f.read()
        socket_5555.send_string(csv_str)
        continue

    elif message == "get_Rcal":
        with open("/home/ubuntu/Software/Rcalibration.csv", "r") as f:
            csv_str = f.read()
        socket_5555.send_string(csv_str)
        continue

    elif message == "get_Ccal":
        with open("/home/ubuntu/Software/Ccalibration.csv", "r") as f:
            csv_str = f.read()
        socket_5555.send_string(csv_str)
        continue

    elif message == "clear_latch":
        # Write to setThreshold.env
        try:
            with open("/home/ubuntu/Software/latchState.env", "w") as f:
                f.write(f"latched=0\n\n")
            reply = "Cleared Latch value in latchState.env"
        except Exception as e:
            reply = f"Failed to write latchState.env: {e}"
            socket_5555.send_string(reply)
            continue
        

    else:
        reply = "Unknown command"

    socket_5555.send_string(reply)
