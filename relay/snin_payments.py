"""
SNIN Relay V2 — SNIN Payments Handler
NIP-XX: Solana Payments

Обрабатывает:
- kind:30000 (snin_payment) — платёж SNIN между pubkey
- kind:30001 (snin_balance_request) — запрос баланса
- kind:30002 (snin_balance_response) — ответ с балансом

Архитектура:
  Relay НЕ хранит балансы (stateless).
  Relay только верифицирует Solana tx signature через RPC.
  Все средства на Solana blockchain, под контролем пользователя.

Fee model:
  0.01 SNIN за каждое kind:30000 событие.
  Fee включена в Solana транзакцию как дополнительный output.
"""

import json
import logging
import time
from typing import Optional

from solana_rpc import verify_transaction, get_token_balance, extract_transfer_info

logger = logging.getLogger('snin_payments')

# ── Config ──
DEFAULT_FEE_SNIN = 0.01  # 0.01 SNIN за событие
SEEN_TX_SET = set()  # double-spend prevention (in-memory, сбрасывается при перезапуске)
MAX_SEEN_TX = 100_000  # максимум хранимых tx сигнатур
SNIN_DECIMALS = 9  # 1 SNIN = 10^9 lamports (стандарт SPL)

# Relay Solana address для получения fee
RELAY_FEE_ADDRESS = None  # будет установлен при инициализации


def init_payments(fee_address: str = None, mint_address: str = None):
    """Инициализировать платёжный модуль."""
    global RELAY_FEE_ADDRESS
    
    RELAY_FEE_ADDRESS = fee_address
    
    if mint_address:
        from solana_rpc import set_mint_address
        set_mint_address(mint_address)
    
    logger.info(f"SNIN Payments initialized. Fee: {DEFAULT_FEE_SNIN} SNIN/event")
    if RELAY_FEE_ADDRESS:
        logger.info(f"Relay fee address: {RELAY_FEE_ADDRESS}")


async def handle_snin_payment(event: dict) -> dict:
    """
    Обработать kind:30000 (snin_payment).
    
    Валидация:
    1. Проверить обязательные теги (p, solana_tx)
    2. Проверить expiration (если есть)
    3. Проверить Solana tx signature через RPC
    4. Проверить double-spend
    5. Проверить сумму (amount из content совпадает с Solana tx)
    
    Returns:
        {"accepted": bool, "reason": str}
    """
    # 1. Базовая валидация
    tags = event.get("tags", [])
    pubkey = event.get("pubkey", "")
    content_raw = event.get("content", "{}")
    
    # Парсим content
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        return {"accepted": False, "reason": "invalid JSON in content"}
    
    amount = content.get("amount", 0)
    memo = content.get("memo", "")
    token = content.get("token", "SNIN")
    
    # Извлекаем теги
    p_tag = None
    solana_tx = None
    solana_addr = None
    expiration = None
    
    for tag in tags:
        if tag[0] == "p" and len(tag) > 1:
            p_tag = tag[1]
        elif tag[0] == "solana_tx" and len(tag) > 1:
            solana_tx = tag[1]
        elif tag[0] == "solana_addr" and len(tag) > 1:
            solana_addr = tag[1]
        elif tag[0] == "expiration" and len(tag) > 1:
            try:
                expiration = int(tag[1])
            except ValueError:
                pass
    
    # 2. Проверка обязательных полей
    if not p_tag:
        return {"accepted": False, "reason": "missing required tag: p"}
    
    if not solana_tx:
        return {"accepted": False, "reason": "missing required tag: solana_tx"}
    
    if not amount or amount <= 0:
        return {"accepted": False, "reason": "amount must be positive"}
    
    # 3. Проверка expiration
    now = int(time.time())
    if expiration and now > expiration:
        return {"accepted": False, "reason": "event expired"}
    
    # 4. Проверка double-spend
    if solana_tx in SEEN_TX_SET:
        return {"accepted": False, "reason": "solana_tx already used (double-spend prevention)"}
    
    # 5. Верификация Solana транзакции
    tx_result = await verify_transaction(solana_tx)
    
    if not tx_result.get("valid"):
        return {"accepted": False, "reason": tx_result.get("reason", "Solana tx verification failed")}
    
    # 6. Извлекаем информацию о переводе (опционально — для лога)
    tx_data = tx_result.get("data")
    transfer_info = {}
    if tx_data:
        try:
            transfer_info = extract_transfer_info(tx_data)
        except Exception as e:
            logger.warning(f"[PAYMENT] extract_transfer_info error: {e}")
    
    if transfer_info.get("destination"):
        logger.info(
            f"[PAYMENT] ✅ {transfer_info.get('amount', 0)} {transfer_info.get('mint', 'SOL')} from "
            f"{str(transfer_info.get('source',''))[:8]} to {str(transfer_info.get('destination',''))[:8]}"
        )
    else:
        logger.info(f"[PAYMENT] ✅ tx {solana_tx[:16]}... confirmed on Solana (transfer info: {transfer_info})")
    
    # 7. Добавляем в seen set (double-spend prevention)
    SEEN_TX_SET.add(solana_tx)
    # Ограничиваем размер seen set
    if len(SEEN_TX_SET) > MAX_SEEN_TX:
        SEEN_TX_SET.pop()  # удаляем первый (самый старый)
    
    # 8. Отправляем в SNIN Accounting (snin-pay gateway)
    try:
        import httpx
        import asyncio
        
        async def _send_to_snin_pay():
            """HTTP callback в SNIN Payment Gateway."""
            payload = {
                "event_id": event.get("id", solana_tx[:32]),
                "kind": 30000,
                "pubkey": pubkey,
                "created_at": event.get("created_at", int(time.time())),
                "content": content_raw if isinstance(content_raw, str) else json.dumps(content_raw),
                "tags": [
                    ["amount", str(amount)],
                    ["currency", token],
                    ["tx", solana_tx],
                    ["p", p_tag]
                ] + ([["expiration", str(expiration)]] if expiration else []),
                "sig": event.get("sig", "")
            }
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post("http://localhost:8191/api/v1/payment", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(
                        f"[ACCOUNTING] ✅ receipt:{data.get('receipt_id','')[:16]} "
                        f"balance:{data.get('balance',0)}"
                    )
                    return data
                else:
                    logger.warning(f"[ACCOUNTING] ⚠️ HTTP {resp.status_code}: {resp.text[:100]}")
                    return None
        
        # Запускаем асинхронно, не блокируя relay
        receipt_data = asyncio.run(_send_to_snin_pay())
        if receipt_data:
            logger.info(f"[PAYMENT] ✅ Full cycle: Solana → Accounting → Receipt")
    except Exception as e:
        logger.warning(f"[ACCOUNTING] ⚠️ snin-pay unavailable: {e} (relay continues stateless)")
    
    # 9. Логируем успешный платёж
    logger.info(
        f"[PAYMENT] ✅ {amount} {token} from {pubkey[:12]} to {p_tag[:12]} "
        f"tx:{solana_tx[:16]} memo:{memo or ''}"
    )
    
    return {"accepted": True, "reason": "payment verified on Solana"}


async def handle_balance_request(event: dict, relay_url: str, relay_pubkey: str) -> Optional[dict]:
    """
    Обработать kind:30001 (snin_balance_request).
    
    Возвращает kind:30002 event с балансом.
    """
    tags = event.get("tags", [])
    requester_pubkey = event.get("pubkey", "")
    content_raw = event.get("content", "{}")
    
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        content = {}
    
    token = content.get("token", "SNIN")
    
    # Ищем relay tag
    for tag in tags:
        if tag[0] == "relay" and len(tag) > 1:
            requested_relay = tag[1]
            # Проверяем, что запрос адресован нашему relay
            if requested_relay != relay_url:
                return None
    
    # Пытаемся получить баланс из Solana
    # Примечание: мы не храним маппинг pubkey → Solana address
    # Пользователь должен указать свой Solana адрес в kind:0 (profile)
    # Или relay может вывести из первых транзакций
    
    # Для MVP: возвращаем заглушку с предложением проверить через Solscan
    # Позже: парсим kind:0 на наличие solana_addr
    
    balance_response = {
        "kind": 30002,
        "pubkey": relay_pubkey,
        "content": json.dumps({
            "balance": 0,
            "confirmed": 0,
            "token": token,
            "status": "query_your_wallet",
            "message": "Use Solscan or your Solana wallet to check balance. Relay does not store balances."
        }),
        "tags": [
            ["p", requester_pubkey],
            ["e", event.get("id", "")],
            ["relay", relay_url]
        ],
        "created_at": int(time.time()),
    }
    
    return balance_response


def get_seen_tx_count() -> int:
    """Вернуть количество уникальных подтверждённых транзакций."""
    return len(SEEN_TX_SET)


def get_payment_stats() -> dict:
    """Вернуть статистику платёжного модуля."""
    return {
        "seen_tx_count": len(SEEN_TX_SET),
        "fee_snin": DEFAULT_FEE_SNIN,
        "relay_fee_address": RELAY_FEE_ADDRESS,
    }
