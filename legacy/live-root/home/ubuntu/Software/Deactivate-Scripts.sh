#!/bin/bash
PID=$(ps aux | grep 'ZMon' | grep -v 'grep' | awk '{print $2}')
if [ ! -z "$PID" ]; then
    sudo kill -9 $PID
    echo "Killed ZMon process with PID $PID"
else
    echo "No running ZMon process found"
fi


PID=$(ps aux | grep 'EVE' | grep -v 'grep' | awk '{print $2}')
if [ ! -z "$PID" ]; then
    sudo kill -9 $PID
    echo "Killed EVE process with PID $PID"
else
    echo "No running EVE process found"
fi

#PID=$(ps aux | grep 'zmq' | grep -v 'grep' | awk '{print $2}')
#if [ ! -z "$PID" ]; then
#    sudo kill -9 $PID
#    echo "Killed zmq process with PID $PID"
#else
#    echo "No running zmq process found"
#fi

