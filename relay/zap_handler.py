"""
SNIN Relay V2 — NIP-57 Zap Handler
Lightning Address → LNURL-pay → Invoice → Zap Receipt

Uses a single Lightning Address for all DAO zaps.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.request
import urllib.parse

logger = logging.getLogger('zap_handler')

# ── Lightning Address Config ──
LIGHTNING_ADDRESS = "brashfoster340@walletofsatoshi.com"
LNURLP_CACHE = {}  # cache lnurlp response
LNURLP_CACHE_TTL = 300  # 5 min


def parse_lightning_address(address: str) -> tuple[str, str]:
    """Parse 'user@domain.com' → (user, domain, lnurlp_url)"""
    parts = address.split("@")
    if len(parts) != 2:
        raise ValueError(f"Invalid lightning address: {address}")
    user, domain = parts
    lnurlp_url = f"https://{domain}/.well-known/lnurlp/{user}"
    return user, domain, lnurlp_url


def fetch_lnurlp(lnurlp_url: str) -> dict | None:
    """Fetch LNURL-pay parameters from the endpoint."""
    global LNURLP_CACHE
    
    now = time.time()
    cached = LNURLP_CACHE.get(lnurlp_url)
    if cached and (now - cached.get("fetched_at", 0)) < LNURLP_CACHE_TTL:
        return cached
    
    try:
        req = urllib.request.Request(
            lnurlp_url,
            headers={"Accept": "application/json", "User-Agent": "SNIN-Relay/2.1"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        
        data["fetched_at"] = now
        LNURLP_CACHE[lnurlp_url] = data
        logger.info(f"⚡ LNURL-pay fetched: {lnurlp_url} (min={data.get('minSendable',0)}msat, max={data.get('maxSendable',0)}msat)")
        return data
    except Exception as e:
        logger.error(f"⚡ LNURL-pay fetch failed: {e}")
        return None


def generate_invoice(lnurlp_data: dict, amount_msat: int, comment: str = "", nostr_pubkey: str = "") -> dict | None:
    """Request an invoice from the LNURL-pay callback."""
    callback = lnurlp_data.get("callback")
    if not callback:
        logger.error("No callback URL in LNURL-pay data")
        return None
    
    params = {
        "amount": str(amount_msat),
        "nostr": nostr_pubkey,
    }
    if comment:
        params["comment"] = comment[:lnurlp_data.get("commentAllowed", 0)]
    
    url = f"{callback}?{urllib.parse.urlencode(params)}"
    
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "SNIN-Relay/2.1"}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        
        if data.get("status") == "ERROR":
            logger.error(f"Invoice error: {data.get('reason', 'unknown')}")
            return None
        
        return data
    except Exception as e:
        logger.error(f"Invoice request failed: {e}")
        return None


def create_zap_receipt(zap_request: dict, payment_preimage: str, invoice: str, amount_msat: int) -> dict:
    """
    Create a kind:9735 Zap Receipt event.
    Based on NIP-57: https://github.com/nostr-protocol/nips/blob/master/57.md
    """
    from nostr_protocol import Keys, Tag, Kind, EventBuilder
    
    # Get the zap request details
    zr_tags = zap_request.get("tags", [])
    zr_pubkey = zap_request.get("pubkey", "")
    zr_content = zap_request.get("content", "")
    
    # Extract 'p' and 'e' from zap request
    p_tag = None
    e_tag = None
    for t in zr_tags:
        if len(t) >= 2:
            if t[0] == "p" and not p_tag:
                p_tag = t[1]
            elif t[0] == "e" and not e_tag:
                e_tag = t[1]
    
    # Build the description hash from the zap request
    description = json.dumps(zap_request, separators=(",", ":"))
    description_hash = hashlib.sha256(description.encode()).hexdigest()
    
    # Tags for zap receipt
    tags = [
        Tag.parse(["p", p_tag or zr_pubkey]),
        Tag.parse(["bolt11", invoice]),
        Tag.parse(["description", description]),
        Tag.parse(["preimage", payment_preimage]),
    ]
    if e_tag:
        tags.append(Tag.parse(["e", e_tag]))
    
    # Amount in msats from the zap request
    tags.append(Tag.parse(["amount", str(amount_msat)]))
    
    # Use the relay's key to sign (we use a relay-level key)
    # For now, we need a signing key. Let's use a generated one per session
    # In production, this would be the relay's own key
    k = Keys.generate()
    
    content = ""
    evt = EventBuilder(Kind(9735), content, tags).to_event(k)
    return json.loads(evt.as_json())


async def handle_zap_request(zap_request: dict, relay_pubkey: str) -> dict:
    """
    Handle a NIP-57 zap request (kind:9734).
    Returns a response dict: {result, event?, error?}
    """
    try:
        # Validate zap request
        if zap_request.get("kind") != 9734:
            return {"result": "error", "error": "not a zap request (kind != 9734)"}
        
        # Extract amount from tags
        amount_msat = 0
        for t in zap_request.get("tags", []):
            if len(t) >= 2 and t[0] == "amount":
                try:
                    amount_msat = int(t[1])
                except ValueError:
                    pass
                break
        
        if amount_msat <= 0:
            return {"result": "error", "error": "invalid amount"}
        
        # Fetch LNURL-pay params
        user, domain, lnurlp_url = parse_lightning_address(LIGHTNING_ADDRESS)
        lnurlp_data = fetch_lnurlp(lnurlp_url)
        
        if not lnurlp_data:
            return {"result": "error", "error": "failed to fetch LNURL-pay params"}
        
        # Validate amount against limits
        min_sendable = lnurlp_data.get("minSendable", 1000)
        max_sendable = lnurlp_data.get("maxSendable", 1000000000)
        
        if amount_msat < min_sendable:
            return {"result": "error", "error": f"amount too small (min {min_sendable} msat)"}
        if amount_msat > max_sendable:
            return {"result": "error", "error": f"amount too large (max {max_sendable} msat)"}
        
        # Get comment from zap request
        comment = ""
        for t in zap_request.get("tags", []):
            if len(t) >= 2 and t[0] == "comment":
                comment = t[1]
                break
        
        # Get the zapper's pubkey
        zapper_pubkey = zap_request.get("pubkey", "")
        
        # Request invoice
        invoice_data = generate_invoice(lnurlp_data, amount_msat, comment, zapper_pubkey)
        
        if not invoice_data or "pr" not in invoice_data:
            return {"result": "error", "error": "failed to generate invoice"}
        
        invoice = invoice_data["pr"]
        
        # Return the zap response with invoice
        # We also create a simulated zap receipt since we can't verify payment
        payment_preimage = "0" * 64  # simulated
        zap_receipt = create_zap_receipt(zap_request, payment_preimage, invoice, amount_msat)
        
        return {
            "result": "ok",
            "invoice": invoice,
            "zap_event": zap_receipt,
        }
        
    except Exception as e:
        logger.error(f"Zap request error: {e}")
        return {"result": "error", "error": str(e)}


def get_lnurlp_response(pubkey: str, relay_name: str) -> dict:
    """
    Generate the LNURL-pay response for /.well-known/lnurlp/{pubkey}.
    NIP-57 compliant: returns metadata with 'text/identifier' and 'text/plain'.
    """
    _, _, lnurlp_url = parse_lightning_address(LIGHTNING_ADDRESS)
    lnurlp_data = fetch_lnurlp(lnurlp_url)
    
    if not lnurlp_data:
        # Fallback: return basic info
        return {
            "status": "ERROR",
            "reason": "Lightning service temporarily unavailable"
        }
    
    # Create the response. We keep the original callback but add our own metadata
    # with the SNIN branding and a fixed 'p' tag description
    metadata = [
        ["text/identifier", LIGHTNING_ADDRESS],
        ["text/plain", f"⚡ Support {relay_name} with Lightning"],
    ]
    
    response = {
        "callback": lnurlp_data.get("callback", ""),
        "maxSendable": lnurlp_data.get("maxSendable", 1000000000),
        "minSendable": lnurlp_data.get("minSendable", 1000),
        "metadata": json.dumps(metadata),
        "commentAllowed": lnurlp_data.get("commentAllowed", 0),
        "tag": "payRequest",
        "allowsNostr": True,
        "nostrPubkey": pubkey,
    }
    
    return response
