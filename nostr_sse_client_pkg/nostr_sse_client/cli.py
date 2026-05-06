#!/usr/bin/env python3
"""
CLI interface for nostr-sse-client.

Usage:
  nostr-sse --relay https://relay.example.com --gen-key
  nostr-sse --relay https://relay.example.com --subscribe '{"kinds":[1],"limit":5}'
  nostr-sse --relay https://relay.example.com --publish "Hello Nostr!"
  nostr-sse --relay https://relay.example.com --nsec nsec1... --inbox
  nostr-sse --relay https://relay.example.com --nsec nsec1... --dm <pubkey> "message"
"""
import json, time, sys
import argparse

from .client import NostrSSEClient, NostrEvent
from .utils import generate_keypair


def print_event(event: NostrEvent, show_content: bool = True):
    """Pretty-print a Nostr event."""
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event.created_at))
    print(f"  ┌─ Kind:{event.kind}  ── {created}")
    print(f"  │ ID:   {event.id[:20]}...")
    print(f"  │ From: {event.pubkey[:20]}...")
    if show_content:
        content = event.content[:120].replace("\n", "\\n")
        print(f"  └─ {content}")
    print()


def cmd_gen_key():
    """Generate a new Nostr keypair."""
    kp = generate_keypair()
    print(f"nsec:       {kp['nsec']}")
    print(f"npub:       {kp['npub']}")
    print(f"privkey:    {kp['privkey_hex']}")
    print(f"pubkey:     {kp['pubkey_hex']}")
    print()
    print("Save your nsec securely! It cannot be recovered.")


def cmd_auth(relay_url: str, nsec: str = None):
    """Perform NIP-42 AUTH authentication."""
    if not nsec:
        print("Error: --auth requires --nsec <key>", file=sys.stderr)
        sys.exit(1)
    
    from .utils import nsec_to_private_key
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    print(f"Authenticating with relay: {relay_url}")
    if client.npub:
        print(f"  As: {client.npub}")
    
    result = client.authenticate()
    if result.get("status") == "ok":
        print(f"  ✅ AUTH success!")
        print(f"  Pubkey: {result.get('pubkey', '?')[:20]}...")
        print(f"  Token:  {client._auth_token[:20]}...")
    else:
        print(f"  ❌ AUTH failed: {result.get('message', 'unknown error')}")
        sys.exit(1)


def cmd_send_dm(relay_url: str, nsec: str, recipient_pubkey: str, message: str):
    """Send an encrypted NIP-04 DM."""
    if not nsec:
        print("Error: --dm requires --nsec <key>", file=sys.stderr)
        sys.exit(1)

    from .utils import nsec_to_private_key
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)

    if client.npub:
        print(f"From: {client.npub}")
    print(f"To:   {recipient_pubkey[:20]}...")
    print(f"Msg:  {message[:60]}{'...' if len(message) > 60 else ''}")
    print()

    result = client.send_dm(recipient_pubkey, message)
    if result.get("status") == "ok":
        print(f"  ✅ DM sent! event_id: {result.get('event_id', '?')[:20]}...")
    else:
        print(f"  ❌ Failed: {result.get('message', 'unknown error')}")
        sys.exit(1)


def cmd_inbox(relay_url: str, nsec: str, limit: int = 10):
    """Show received DMs."""
    if not nsec:
        print("Error: --inbox requires --nsec <key>", file=sys.stderr)
        sys.exit(1)

    from .utils import nsec_to_private_key
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)

    if client.npub:
        print(f"Inbox for: {client.npub}")
    print(f"Relay: {relay_url}")
    print()

    events = client.fetch_inbox(limit)
    if not events:
        print("  📭 No DMs found")
        return

    print(f"  📨 {len(events)} DM(s)\n")
    for i, ev in enumerate(events, 1):
        decrypted = client.decrypt_dm(ev.content, ev.pubkey)
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ev.created_at))
        print(f"  [{i}] From: {ev.pubkey[:20]}...")
        print(f"       At:  {created}")
        if decrypted:
            print(f"       📝: {decrypted[:200]}")
        else:
            print(f"       🔒 (cannot decrypt — wrong key)")
        print()


def cmd_info(relay_url: str):
    """Fetch NIP-11 info from relay."""
    try:
        resp = requests.get(
            relay_url,
            headers={"Accept": "application/nostr+json"},
            timeout=15
        )
        if resp.status_code == 200:
            info = resp.json()
            print(f"Relay:      {relay_url}")
            print(f"Name:       {info.get('name', '?')}")
            print(f"Software:   {info.get('software', '?')}")
            print(f"Version:    {info.get('version', '?')}")
            print(f"Pubkey:     {info.get('pubkey', '?')[:20]}...")
            print(f"Contact:    {info.get('contact', '?')}")
            print(f"Supported NIPs: {len(info.get('supported_nips', []))}")
            print(f"  {info.get('supported_nips', [])}")
            
            limitation = info.get('limitation', {})
            if limitation:
                print(f"Max events: {limitation.get('max_event_tags', '-')}")
                print(f"Max filters: {limitation.get('max_subid_filters', '-')}")
                print(f"Auth req:   {limitation.get('auth_required', False)}")
            
            stats = info.get('stats', {})
            if stats:
                print(f"Events:     {stats.get('num_events', '-')}")
                print(f"Authors:    {stats.get('num_authors', '-')}")
            
            return info
        else:
            print(f"HTTP {resp.status_code}: relay returned {resp.text[:200]}")
    except Exception as e:
        print(f"Error: {e}")


def cmd_dashboard(relay_url: str):
    """Show relay dashboard."""
    import requests
    try:
        dash_url = relay_url.rstrip("/") + "/"
        resp = requests.get(dash_url, timeout=15)
        if resp.status_code == 200:
            html = resp.text
            for line in html.split("\n"):
                if "info-row" in line or "count" in line or "peers" in line or "ipfs" in line.lower():
                    stripped = line.strip()
                    if stripped:
                        print(f"  {stripped}")
        else:
            print(f"Dashboard HTTP {resp.status_code}")
    except Exception as e:
        print(f"Dashboard error: {e}")

    # Also try /api/st
    try:
        api_url = relay_url.rstrip("/") + "/api/st"
        resp = requests.get(api_url, timeout=10)
        if resp.status_code == 200:
            st = resp.json()
            print(f"\n  /api/st:")
            for k, v in st.items():
                if isinstance(v, dict):
                    print(f"    {k}:")
                    for sk, sv in v.items():
                        print(f"      {sk}: {sv}")
                else:
                    print(f"    {k}: {v}")
    except Exception:
        pass


def cmd_ipfs(relay_url: str):
    """Show IPFS stats."""
    import requests
    try:
        api_url = relay_url.rstrip("/") + "/api/st"
        resp = requests.get(api_url, timeout=10)
        if resp.status_code == 200:
            st = resp.json()
            ipfs_data = st.get("ipfs_stats", st)
            for k, v in ipfs_data.items():
                print(f"  {k}: {v}")
        else:
            print(f"HTTP {resp.status_code}")
    except Exception as e:
        print(f"Error: {e}")


def cmd_subscribe(relay_url: str, filters: dict, nsec: str = None,
                   count: int = None, follow: bool = False):
    """Subscribe and print events."""
    from .utils import nsec_to_private_key
    privkey = None
    if nsec:
        privkey = nsec_to_private_key(nsec)
    
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    if client.npub:
        print(f"Connected as: {client.npub[:20]}...")
    print(f"Relay: {relay_url}")
    print(f"Filters: {json.dumps(filters, ensure_ascii=False)}")
    if follow:
        print(f"Mode: LIVE (follow enabled, auto-reconnect on)")
    else:
        print(f"Listening... (Ctrl+C to stop)")
    print()
    
    received = 0
    try:
        for event, eose_flag in client.subscribe(filters, follow=follow):
            if eose_flag:
                if not follow:
                    print(f"  [EOSE] — end of stored events ({received} received)")
                    return
                continue
            
            print_event(event)
            received += 1
            
            if count and received >= count and not follow:
                return
    except KeyboardInterrupt:
        print("\n  Stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Nostr SSE Client — Nostr over HTTP SSE, no WebSocket needed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--relay", "-r", default="https://relay-snin.v2.site",
                        help="Relay URL (default: https://relay-snin.v2.site)")
    parser.add_argument("--nsec", help="Private key in nsec format")
    parser.add_argument("--gen-key", action="store_true", help="Generate new Nostr keypair")
    parser.add_argument("--subscribe", "-s", help="Subscribe with JSON filter")
    parser.add_argument("--publish", "-p", help="Publish a text event")
    parser.add_argument("--kind", type=int, default=1, help="Event kind for publish (default: 1)")
    parser.add_argument("--count", "-n", type=int, help="Number of events to receive")
    parser.add_argument("--info", action="store_true", help="Get relay NIP-11 info")
    parser.add_argument("--dashboard", action="store_true", help="Show relay dashboard")
    parser.add_argument("--ipfs", action="store_true", help="Show IPFS stats")
    parser.add_argument("--follow", "-f", action="store_true",
                        help="Follow SSE stream continuously (no EOSE stop)")
    parser.add_argument("--auth", action="store_true",
                        help="Authenticate via NIP-42 AUTH (requires --nsec)")
    parser.add_argument("--dm", nargs=2, metavar=("PUBKEY", "MESSAGE"),
                        help="Send encrypted DM to a pubkey (requires --nsec)")
    parser.add_argument("--inbox", action="store_true",
                        help="Show received DMs (kind:4, requires --nsec)")
    parser.add_argument("--dm-limit", type=int, default=10,
                        help="Max DMs to show in --inbox (default: 10)")
    
    args = parser.parse_args()
    
    if args.gen_key:
        cmd_gen_key()
    elif args.info:
        cmd_info(args.relay)
    elif args.dashboard:
        cmd_dashboard(args.relay)
    elif args.ipfs:
        cmd_ipfs(args.relay)
    elif args.auth:
        cmd_auth(args.relay, args.nsec)
    elif args.dm:
        cmd_send_dm(args.relay, args.nsec, args.dm[0], args.dm[1])
    elif args.inbox:
        cmd_inbox(args.relay, args.nsec, args.dm_limit)
    elif args.subscribe:
        try:
            filters = json.loads(args.subscribe)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON filter: {e}", file=sys.stderr)
            sys.exit(1)
        cmd_subscribe(args.relay, filters, args.nsec, args.count, args.follow)
    elif args.publish:
        cmd_publish(args.relay, args.nsec, args.publish, args.kind)
    else:
        parser.print_help()


def cmd_publish(relay_url: str, nsec: str = None, content: str = "", kind: int = 1):
    """Publish a text event."""
    from .utils import nsec_to_private_key
    
    if not nsec:
        print("Error: --publish requires --nsec <key>", file=sys.stderr)
        sys.exit(1)
    
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    print(f"Relay: {relay_url}")
    if client.npub:
        print(f"As:    {client.npub}")
    print(f"Kind:  {kind}")
    print(f"Content: {content}")
    print()
    
    result = client.sign_and_publish(content, kind=kind)
    if result.get("status") == "ok":
        print(f"✅ Published! event_id: {result.get('event_id', '?')[:20]}...")
    else:
        print(f"❌ Failed: {result.get('message', 'unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
