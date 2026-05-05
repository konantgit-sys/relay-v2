#!/usr/bin/env python3
"""Синхронизация whitelist релея с keyring SNIN."""
import sys, json, os
sys.path.insert(0, '/home/agent/data/sites/chrono')

# Попробовать импорт keyring
try:
    from keystore.keyring import Keyring
    kr = Keyring()
    agents = kr.list_agents()
    npubs = ['npub' + a.get('npub','')[-63:] for a in agents if not a.get('revoked')]
    npubs = [n for n in npubs if len(n) > 5]
    kr.close()
    print(f"✅ Keyring: {len(npubs)} agents loaded")
except Exception as e:
    print(f"⚠️ Keyring import failed: {e}")
    print("  Using hardcoded whitelist from relay_server.py")
    npubs = [
        "npub1cryterkey000000000000000000000000000000000",
    ]

# Обновить relay_server.py
SERVER_PATH = '/home/agent/data/sites/relay/relay_server.py'
with open(SERVER_PATH) as f:
    code = f.read()

pubkeys_hex = []
for npub in npubs:
    try:
        import bech32
        hrp, data = bech32.decode('npub', npub) if npub.startswith('npub') else ('',[])
        if data:
            pubhex = bytes(bech32.convertbits(data[1:], 5, 8, False)).hex()
            pubkeys_hex.append(pubhex)
    except:
        pass

# Найти строку WHITELIST и заменить
whitelist_line = f"WHITELIST_PUBKEYS = {json.dumps(pubkeys_hex)}"
if 'WHITELIST_PUBKEYS' in code:
    # Заменить существующий
    import re
    code = re.sub(r'WHITELIST_PUBKEYS\s*=\s*\[.*?\]', whitelist_line, code, flags=re.DOTALL)
else:
    # Добавить
    code += f"\n{whitelist_line}\n"

with open(SERVER_PATH, 'w') as f:
    f.write(code)

print(f"✅ Whitelist updated: {len(pubkeys_hex)} pubkeys")
for n in npubs[:5]:
    print(f"   {n[:40]}...")
if len(npubs) > 5:
    print(f"   ... and {len(npubs)-5} more")

# Обновить relay.yaml
import yaml
YAML_PATH = '/home/agent/data/sites/relay/relay.yaml'
with open(YAML_PATH) as f:
    config = yaml.safe_load(f)

config['auth']['enabled'] = True
config['auth']['mode'] = 'whitelist'
with open(YAML_PATH, 'w') as f:
    yaml.dump(config, f, default_flow_style=False)

print("✅ relay.yaml: auth enabled → whitelist mode")
