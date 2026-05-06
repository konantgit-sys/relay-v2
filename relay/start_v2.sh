#!/bin/bash
cd /home/agent/data/sites/relay
exec python3 relay_server_v2.py >> relay_v2.log 2>&1
