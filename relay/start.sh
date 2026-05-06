#!/bin/bash
# SNIN Relay — auto-start with IPFS pubsub + adaptive fanout v4 + NIP-65 + WSS tunnel
cd /home/agent/data/sites/relay

# Kill old processes
pkill -f "relay_server_v2.py" 2>/dev/null
pkill -f "ws_gateway.py" 2>/dev/null
pkill -f "cloudflared" 2>/dev/null
pkill -f "ipfs daemon" 2>/dev/null
sleep 2

# 0. Start IPFS daemon with pubsub experiment
export IPFS_PATH=/home/agent/data/.ipfs
export PATH="/home/agent/data:$PATH"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY

if [ -f "/home/agent/data/ipfs" ]; then
    nohup /home/agent/data/ipfs daemon --enable-pubsub-experiment > /tmp/ipfs.log 2>&1 &
    echo "IPFS PID: $!"
    sleep 8
fi

# 1. Start relay on 8198
nohup python3 relay_server_v2.py > /tmp/relay_v2.log 2>&1 &
RELAY_PID=$!
echo "Relay PID: $RELAY_PID"
sleep 4

# 2. Start gateway on 9900
nohup python3 ws_gateway.py > /tmp/ws_gateway.log 2>&1 &
GATEWAY_PID=$!
echo "Gateway PID: $GATEWAY_PID"
sleep 3

# 3. Start Cloudflare Tunnel
if [ -f "/tmp/cloudflared" ]; then
    nohup /tmp/cloudflared tunnel --url http://127.0.0.1:9900 --no-autoupdate --protocol http2 > /tmp/cloudflare_tunnel.log 2>&1 &
    echo "Cloudflare PID: $!"

    for i in $(seq 1 12); do
        TUNNEL_URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" /tmp/cloudflare_tunnel.log | tail -1)
        if [ -n "$TUNNEL_URL" ]; then
            echo "Tunnel URL: $TUNNEL_URL"
            echo "$TUNNEL_URL" > /tmp/tunnel_url.txt
            break
        fi
        sleep 2
    done
fi

echo "Relay + IPFS + Gateway + Tunnel started"
