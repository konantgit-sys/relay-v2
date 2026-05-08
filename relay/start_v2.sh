#!/bin/bash
cd "$(dirname "$0")"
exec python3 relay_server_v2.py >> relay_v2.log 2>&1
