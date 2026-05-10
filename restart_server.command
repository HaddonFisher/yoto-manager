#!/bin/bash
cd "$(dirname "$0")"
pkill -f "python3 server.py" || true
sleep 1
python3 server.py
