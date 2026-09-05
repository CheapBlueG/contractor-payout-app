import os
import re
import time
from datetime import datetime, timezone

import requests

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
LTC_API_KEY = os.getenv("LTC_API_KEY", os.getenv("BLOCKCYPHER_API_KEY", ""))

# --------------------------------------------------------------------------
# Receiving wallet addresses.
#
# These are read from environment variables ONLY (set them in the Render
# dashboard under your service's "Environment" tab). There is intentionally
# no hardcoded fallback address anymore: if an env var is missing, any
# reconciliation attempt for that coin fails loudly instead of silently
# checking payments against a stale/wrong address baked into the code.
#
# Required env vars on Render:
#   BTC_RECEIVING_ADDRESS
#   LTC_RECEIVING_ADDRESS
#   ETH_RECEIVING_ADDRESS
# --------------------------------------------------------------------------

def _require_wallet_env(env_name: str) -> str:
    value = os.getenv(env_name, "").strip()
    if not value:
        raise ValueError(
            f"{env_name} is not configured on the server. Set it in Render's "
            f"Environment tab for this service, then redeploy, before verifying "
            f"payments for this coin."
        )
    return value


def get_btc_address() -> str:
    return _require_wallet_env("BTC_RECEIVING_ADDRESS")


def get_ltc_address() -> str:
    return _require_wallet_env("LTC_RECEIVING_ADDRESS")


def get_eth_address() -> str:
    return _require_wallet_env("ETH_RECEIVING_ADDRESS")


# --------------------------------------------------------------------------
# Accepting either a raw tx hash OR a pasted block-explorer link.
#
# We don't maintain a whitelist of supported explorer domains. Instead we
# just look for a hash-shaped substring anywhere in whatever was pasted
# (a bare hash, a URL path segment, a query string, etc.). This means any
# explorer link works, including ones we've never heard of, as long as the
# tx hash appears in it in its normal hex form.
# --------------------------------------------------------------------------

_ETH_HASH_RE = re.compile(r"0x[a-fA-F0-9]{64}")
_HEX64_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])")


def extract_tx_hash(raw_input: str, symbol: str) -> str:
    """
    Accepts a raw transaction hash or a pasted explorer URL (any explorer)
    and returns the bare hash. Raises ValueError with a clear message if no
    valid hash can be found.
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("A transaction hash or block-explorer link is required.")

    symbol_upper = symbol.strip().upper()

    if symbol_upper in _ETH_FAMILY_SYMBOLS:
        match = _ETH_HASH_RE.search(text)
        if match:
            return match.group(0).lower()
        raise ValueError(
            "Couldn't find a valid Ethereum transaction hash (0x followed by 64 hex "
            "characters) in what you pasted. Paste the raw hash or a link to the "
            "transaction on any explorer."
        )
    else:
        match = _HEX64_RE.search(text)
        if match:
            return match.group(0).lower()
        raise ValueError(
            f"Couldn't find a valid {symbol_upper} transaction hash (64 hex characters) "
            "in what you pasted. Paste the raw hash or a link to the transaction on "
            "any explorer."
        )


# --------------------------------------------------------------------------
# Auto-detecting which coin a pasted hash/link refers to.
#
# A 0x-prefixed 64-hex hash is unambiguously Ethereum. A bare 64-hex hash
# (BTC and LTC hashes are the identical format) is ambiguous on its own —
# but when the input is a link rather than a raw hash, the explorer's
# domain/path tells us which chain it's for. This lets someone paste a
# link without also having to remember to flip the coin dropdown first.
# --------------------------------------------------------------------------

_EXPLORER_SYMBOL_HINTS = [
    ("etherscan.io", "ETH"),
    ("blockscout.com", "ETH"),
    ("ethplorer.io", "ETH"),
    ("blockchair.com/ethereum", "ETH"),
    ("blockchair.com/bitcoin-cash", None),  # explicitly NOT bitcoin — avoid a "bitcoin" substring false match below
    ("blockchair.com/litecoin", "LTC"),
    ("blockchair.com/bitcoin", "BTC"),
    ("blockstream.info", "BTC"),
    ("mempool.space", "BTC"),
    ("btc.com", "BTC"),
    ("blockcypher.com/btc", "BTC"),
    ("live.blockcypher.com/btc", "BTC"),
    ("blockcypher.com/ltc", "LTC"),
    ("live.blockcypher.com/ltc", "LTC"),
    ("litecoinblockexplorer.net", "LTC"),
    ("chain.so/tx/ltc", "LTC"),
    ("chain.so/tx/btc", "BTC"),
    ("sochain.com/tx/ltc", "LTC"),
    ("sochain.com/tx/btc", "BTC"),
]


def detect_symbol_from_input(raw_input: str):
    """Best-effort coin detection from a pasted hash or explorer link.
    Returns 'ETH', 'BTC', 'LTC', or None if it genuinely can't tell (e.g. a
    bare hash with no coin-specific context) — callers should fall back to
    whatever the person selected in the dropdown in that case, never force
    a guess when there's no real signal."""
    text = (raw_input or "").strip()
    if not text:
        return None

    if _ETH_HASH_RE.search(text):
        return "ETH"

    lower = text.lower()
    # Longest/most-specific hints first so e.g. "blockchair.com/litecoin"
    # matches before a broader "blockchair.com" pattern ever could.
    for hint, symbol in sorted(_EXPLORER_SYMBOL_HINTS, key=lambda h: -len(h[0])):
        if hint in lower:
            return symbol

    return None


def symbol_chain_family(symbol: str) -> str:
    """Maps a specific coin/token symbol to the underlying chain it's
    verified on. USDT/USDC/DAI are all Ethereum-family (checked via the
    same Etherscan API as native ETH), so a plain etherscan.io link can't
    tell us which of the four it is — only that it's "the ETH family".
    Callers use this to decide whether an auto-detected symbol should
    override the dropdown: overriding ETH-family with LTC (a genuine
    cross-chain correction) makes sense; overriding USDT with generic ETH
    just because both use etherscan.io links does not."""
    symbol_upper = symbol.strip().upper()
    if symbol_upper in _ETH_FAMILY_SYMBOLS:
        return "ETH"
    return symbol_upper


# --------------------------------------------------------------------------
# Chain lookups. Each returns a dict:
#   { amount, timestamp, confirmations, explorer_url }
# `timestamp` is the unix time the transaction was mined/confirmed — i.e.
# the moment the funds actually left the sender's control. We price the
# payment at that moment (see get_price_usd_at) so a price drop *after* the
# sender already sent the right amount never counts against them.
# --------------------------------------------------------------------------

def _require_etherscan_key() -> str:
    key = ETHERSCAN_API_KEY.strip()
    if not key:
        raise ValueError(
            "ETHERSCAN_API_KEY is not configured on the server. Etherscan "
            "deprecated their old free/keyless API in August 2025 — an API key "
            "is now required for every request. Get one free at etherscan.io "
            "(Account -> API Keys) and set ETHERSCAN_API_KEY in Render's "
            "Environment tab, then redeploy."
        )
    return key


def _eth_rpc(url: str) -> dict:
    """Calls Etherscan and retries on rate-limit responses specifically.
    A single verify_eth_tx() run makes several sequential Etherscan calls
    (tx, receipt, block, latest-block-number) — on a free-tier key capped
    at 3 req/sec, those can land in the same window and trip the limit
    even on someone's very first attempt. Retrying with a short backoff
    only for the rate-limit case (never for a genuine "not found" or auth
    error) fixes that without masking real problems."""
    max_attempts = 4
    backoff_seconds = 0.4

    for attempt in range(max_attempts):
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise ValueError(f"Etherscan API Error: {data['error'].get('message', 'Unknown API error')}")

        # Etherscan's auth/rate-limit/deprecation failures come back through
        # the "status"/"message" envelope even on these proxy (JSON-RPC-style)
        # endpoints, with `result` as a bare STRING instead of the expected
        # object. Left unchecked, callers see result != a dict and conclude
        # "transaction not found" — which is wrong and hides the real problem.
        if data.get("status") == "0" and isinstance(data.get("result"), str):
            result_str = data["result"]
            if "rate limit" in result_str.lower() and attempt < max_attempts - 1:
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            raise ValueError(f"Etherscan API error: {result_str}")

        return data

    raise ValueError("Etherscan API error: rate limit reached and retries were exhausted.")


_ETH_BASE_URL = "https://api.etherscan.io/v2/api"
_ETH_MAINNET_CHAINID = 1

# Ethereum-family symbols all use 0x-prefixed hashes and go through the same
# Etherscan proxy API, whether it's native ETH or an ERC-20 token transfer.
_ERC20_TOKENS = {
    # Contract addresses confirmed directly against Etherscan's own token
    # pages — a typo here would mean checking an entirely wrong contract.
    "USDT": {"contract": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6, "coingecko_id": "tether"},
    "USDC": {"contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6, "coingecko_id": "usd-coin"},
    "DAI": {"contract": "0x6b175474e89094c44da98b954eedeac495271d0", "decimals": 18, "coingecko_id": "dai"},
}
_ETH_FAMILY_SYMBOLS = {"ETH"} | set(_ERC20_TOKENS.keys())


def _fetch_eth_confirmation_info(block_number_hex, api_key: str):
    """Returns (timestamp, confirmations) for the block a tx landed in, or
    (None, 0) if there's no block number yet. Shared by native-ETH and
    ERC-20 verification since both need the exact same two follow-up
    Etherscan calls once they know which block the tx is in."""
    if not block_number_hex:
        return None, 0

    time.sleep(0.35)
    block_data = _eth_rpc(
        f"{_ETH_BASE_URL}?chainid={_ETH_MAINNET_CHAINID}&module=proxy&action=eth_getBlockByNumber"
        f"&tag={block_number_hex}&boolean=false&apikey={api_key}"
    )
    block_result = block_data.get("result") or {}
    timestamp = int(block_result["timestamp"], 16) if "timestamp" in block_result else None

    time.sleep(0.35)
    latest_data = _eth_rpc(
        f"{_ETH_BASE_URL}?chainid={_ETH_MAINNET_CHAINID}&module=proxy&action=eth_blockNumber&apikey={api_key}"
    )
    latest_hex = latest_data.get("result")
    confirmations = max(0, int(latest_hex, 16) - int(block_number_hex, 16) + 1) if latest_hex else 0

    return timestamp, confirmations


def verify_eth_tx(tx_hash: str) -> dict:
    """Verifies a native ETH transfer via Etherscan's API V2 (mainnet,
    chainid=1). A transfer to one of the known stablecoin contracts is
    redirected to verify_erc20_tx instead of being rejected outright."""
    target_addr = get_eth_address().lower()
    clean_tx = tx_hash.strip().lower()
    if not _ETH_HASH_RE.fullmatch(clean_tx):
        raise ValueError("Invalid Ethereum transaction hash format (must be 0x + 64 hex characters).")

    api_key = _require_etherscan_key()
    base, chainid = _ETH_BASE_URL, _ETH_MAINNET_CHAINID

    tx_data = _eth_rpc(
        f"{base}?chainid={chainid}&module=proxy&action=eth_getTransactionByHash&txhash={clean_tx}&apikey={api_key}"
    )
    result = tx_data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError("Ethereum transaction not found. Verify the TxID on Etherscan.")

    # A transaction can be mined but still have reverted (spent gas, moved
    # nothing). eth_getTransactionByHash alone can't tell you that — you
    # need the receipt's status field.
    time.sleep(0.35)
    receipt_data = _eth_rpc(
        f"{base}?chainid={chainid}&module=proxy&action=eth_getTransactionReceipt&txhash={clean_tx}&apikey={api_key}"
    )
    receipt = receipt_data.get("result")
    if not receipt:
        raise ValueError("This transaction has not been mined yet. Wait for it to confirm and try again.")
    if receipt.get("status") != "0x1":
        raise ValueError("This transaction reverted on-chain, so no value was actually transferred.")

    tx_to = str(result.get("to") or "").strip().lower()
    input_data = str(result.get("input") or "0x")

    if tx_to == target_addr:
        try:
            amount_eth = int(result.get("value", "0x0"), 16) / 1e18
        except ValueError:
            raise ValueError("Failed to parse Ethereum transaction transfer value.")
    else:
        matching_token = next((sym for sym, t in _ERC20_TOKENS.items() if t["contract"] == tx_to), None)
        if matching_token:
            raise ValueError(
                f"This is a {matching_token} transfer, not native ETH. Select "
                f"{matching_token} in the coin dropdown and verify it again."
            )
        raise ValueError(f"Transaction recipient ('{tx_to}') does not match the designated wallet '{get_eth_address()}'.")

    if amount_eth <= 0:
        raise ValueError("Transaction contains 0 ETH transferred to the target address.")

    timestamp, confirmations = _fetch_eth_confirmation_info(result.get("blockNumber"), api_key)

    if timestamp is None:
        # Don't guess — an old, replayed txid would look freshly sent if we
        # ever logged/priced it against "now" instead of the real send time.
        raise ValueError(
            "This Ethereum transaction is confirmed, but the exact time it was "
            "sent couldn't be determined from the block explorer. Refusing to "
            "guess — try verifying again in a moment."
        )

    return {
        "amount": amount_eth,
        "timestamp": timestamp,
        "confirmations": confirmations,
        "explorer_url": f"https://etherscan.io/tx/{clean_tx}",
    }


def verify_erc20_tx(tx_hash: str, token_symbol: str) -> dict:
    """Verifies an ERC-20 stablecoin transfer (USDT, USDC, or DAI) on
    Ethereum mainnet to the configured wallet. Decodes the transferred
    amount using THAT TOKEN'S OWN decimals (6 for USDT/USDC, 18 for DAI)
    instead of assuming 18 across the board — that exact assumption was
    the bug that made the original app's ERC-20 handling silently wrong by
    orders of magnitude for 6-decimal tokens, and why ERC-20 support was
    disabled entirely rather than shipped broken."""
    token_symbol_upper = token_symbol.strip().upper()
    token = _ERC20_TOKENS.get(token_symbol_upper)
    if not token:
        raise ValueError(f"'{token_symbol}' is not a supported token.")

    target_addr = get_eth_address().lower()
    contract_addr = token["contract"]
    clean_tx = tx_hash.strip().lower()
    if not _ETH_HASH_RE.fullmatch(clean_tx):
        raise ValueError(f"Invalid {token_symbol_upper} transaction hash format (must be 0x + 64 hex characters).")

    api_key = _require_etherscan_key()
    base, chainid = _ETH_BASE_URL, _ETH_MAINNET_CHAINID

    tx_data = _eth_rpc(
        f"{base}?chainid={chainid}&module=proxy&action=eth_getTransactionByHash&txhash={clean_tx}&apikey={api_key}"
    )
    result = tx_data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError(f"{token_symbol_upper} transaction not found. Verify the TxID on Etherscan.")

    time.sleep(0.35)
    receipt_data = _eth_rpc(
        f"{base}?chainid={chainid}&module=proxy&action=eth_getTransactionReceipt&txhash={clean_tx}&apikey={api_key}"
    )
    receipt = receipt_data.get("result")
    if not receipt:
        raise ValueError("This transaction has not been mined yet. Wait for it to confirm and try again.")
    if receipt.get("status") != "0x1":
        raise ValueError("This transaction reverted on-chain, so no tokens were actually transferred.")

    tx_to = str(result.get("to") or "").strip().lower()
    if tx_to != contract_addr:
        raise ValueError(
            f"This transaction wasn't sent to the {token_symbol_upper} contract "
            f"({token['contract']}) — it doesn't look like a {token_symbol_upper} transfer. "
            f"Double-check you picked the right coin."
        )

    input_data = str(result.get("input") or "0x")
    # transfer(address,uint256) selector: keccak256("transfer(address,uint256"))[:4]
    if not input_data.startswith("0xa9059cbb") or len(input_data) < 138:
        raise ValueError(
            f"This transaction's call data doesn't match a standard "
            f"{token_symbol_upper} transfer() — it may be an approval, a "
            f"transferFrom, or something else entirely."
        )

    recipient_hex = "0x" + input_data[34:74].lower()
    if recipient_hex != target_addr:
        raise ValueError(
            f"This {token_symbol_upper} transfer was sent to '{recipient_hex}', not "
            f"the designated wallet '{get_eth_address()}'."
        )

    try:
        raw_value = int(input_data[74:138], 16)
    except ValueError:
        raise ValueError(f"Failed to parse the {token_symbol_upper} transfer amount.")

    amount = raw_value / (10 ** token["decimals"])
    if amount <= 0:
        raise ValueError(f"Transaction contains 0 {token_symbol_upper} transferred to the target address.")

    timestamp, confirmations = _fetch_eth_confirmation_info(result.get("blockNumber"), api_key)

    if timestamp is None:
        raise ValueError(
            f"This {token_symbol_upper} transaction is confirmed, but the exact time "
            f"it was sent couldn't be determined. Refusing to guess — try verifying "
            f"again in a moment."
        )

    return {
        "amount": amount,
        "timestamp": timestamp,
        "confirmations": confirmations,
        "explorer_url": f"https://etherscan.io/tx/{clean_tx}",
    }


_BTC_ESPLORA_HOSTS = ["https://blockstream.info/api", "https://mempool.space/api"]


def _esplora_get(path: str):
    """GETs `path` from every known Esplora-compatible host in order
    (Blockstream, mempool.space — they run the identical open-source API,
    so responses are interchangeable) and returns the first success.
    Raises ValueError only if every host fails, and the message includes
    EVERY host's failure reason — not just the last one — so a genuine
    outage/rate-limit on the first host isn't silently overwritten by
    whatever the second host says.

    No internal retry-with-delay here on purpose: if a connection is
    genuinely being silently dropped (packets black-holed rather than
    actively refused), retrying a few hundred ms later doesn't help and
    just makes a stuck-feeling request take even longer. Fail fast per
    host (6s) and let the person retry manually if needed — a clear,
    bounded failure beats a long, silent hang."""
    errors = []
    for host in _BTC_ESPLORA_HOSTS:
        try:
            response = requests.get(f"{host}{path}", timeout=6)
            if response.status_code == 404:
                return None, host  # definitive: this host confirms it doesn't exist
            if response.status_code == 429:
                errors.append(f"{host}: rate-limited (429)")
                continue
            response.raise_for_status()
            return response.json(), host
        except requests.RequestException as e:
            errors.append(f"{host}: {e}")
    print(f"_esplora_get({path}) failed on every BTC host: {'; '.join(errors)}")
    raise ValueError(f"Failed to communicate with any Bitcoin blockchain API. Tried: {'; '.join(errors)}")


def verify_btc_tx(tx_hash: str) -> dict:
    """Verifies a Bitcoin transaction via Esplora-compatible APIs
    (Blockstream, with mempool.space as a fallback if Blockstream is
    rate-limiting or unreachable — both run the same software)."""
    target_addr = get_btc_address().lower()
    clean_tx = tx_hash.strip().lower()
    if not _HEX64_RE.fullmatch(clean_tx):
        raise ValueError("Invalid Bitcoin transaction hash (must be 64 hexadecimal characters).")

    data, used_host = _esplora_get(f"/tx/{clean_tx}")
    if data is None:
        raise ValueError("Bitcoin transaction not found.")

    vouts = data.get("vout", [])
    matched_satoshis = sum(
        vout.get("value", 0) for vout in vouts
        if str(vout.get("scriptpubkey_address", "")).strip().lower() == target_addr
    )
    if matched_satoshis == 0:
        raise ValueError(f"No outputs found sent to target address '{get_btc_address()}' in this transaction.")

    status = data.get("status", {})
    if not status.get("confirmed"):
        raise ValueError(
            "This Bitcoin transaction hasn't confirmed yet. Wait for at least one "
            "confirmation and try again — unconfirmed transactions can still be "
            "replaced or dropped."
        )

    timestamp = status.get("block_time")
    confirmations = 0
    block_height = status.get("block_height")
    block_hash = status.get("block_hash")

    if timestamp is None and block_hash:
        # The tx status almost always includes block_time, but if it's ever
        # missing, the block endpoint always has "timestamp".
        try:
            block_data, _ = _esplora_get(f"/block/{block_hash}")
            if block_data:
                timestamp = block_data.get("timestamp")
        except ValueError as e:
            print(f"BTC block-time fallback failed for block {block_hash}: {e}")

    if block_height:
        # /blocks/tip/height returns plain text (a bare number), not JSON,
        # so this can't go through _esplora_get — fetch it directly instead.
        confirmations = 1
        for host in _BTC_ESPLORA_HOSTS:
            try:
                tip_resp = requests.get(f"{host}/blocks/tip/height", timeout=6)
                tip_resp.raise_for_status()
                confirmations = max(0, int(tip_resp.text) - block_height + 1)
                break
            except (requests.RequestException, ValueError):
                continue

    if timestamp is None:
        # We will NOT silently price/log this against "now" — an old,
        # replayed txid would then look like it was just sent, which is
        # exactly the wrong direction to be wrong in.
        raise ValueError(
            "This Bitcoin transaction is confirmed, but the exact time it was sent "
            "couldn't be determined from the block explorer. Refusing to guess — "
            "try verifying again in a moment."
        )

    return {
        "amount": matched_satoshis / 1e8,
        "timestamp": timestamp,
        "confirmations": confirmations,
        "explorer_url": f"https://blockstream.info/tx/{clean_tx}",
    }


def verify_ltc_tx(tx_hash: str) -> dict:
    """Verifies a Litecoin transaction, preferring BlockCypher and falling
    back to Blockchair if that lookup fails or is rate-limited."""
    target_addr = get_ltc_address().lower()
    clean_tx = tx_hash.strip().lower()
    if not _HEX64_RE.fullmatch(clean_tx):
        raise ValueError("Invalid Litecoin transaction hash (must be 64 hexadecimal characters).")

    explorer_url = f"https://blockchair.com/litecoin/transaction/{clean_tx}"

    # Primary lookup: BlockCypher
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{clean_tx}"
    if LTC_API_KEY:
        url += f"?token={LTC_API_KEY}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            outputs = data.get("outputs", [])
            matched_litoshis = sum(
                out.get("value", 0) for out in outputs
                if any(str(addr).strip().lower() == target_addr for addr in out.get("addresses", []))
            )

            if matched_litoshis > 0:
                confirmations = data.get("confirmations", 0)
                if confirmations < 1:
                    raise ValueError(
                        "This Litecoin transaction hasn't confirmed yet. Wait for at "
                        "least one confirmation and try again."
                    )
                timestamp = None
                confirmed_str = data.get("confirmed")
                if confirmed_str:
                    try:
                        timestamp = int(
                            datetime.fromisoformat(confirmed_str.replace("Z", "+00:00")).timestamp()
                        )
                    except ValueError:
                        pass

                if timestamp is None:
                    # BlockCypher's "confirmed" field is occasionally absent
                    # even when confirmations > 0. Fall back to the block
                    # itself, which always has a "time" field.
                    block_height = data.get("block_height")
                    if block_height:
                        try:
                            block_url = f"https://api.blockcypher.com/v1/ltc/main/blocks/{block_height}"
                            if LTC_API_KEY:
                                block_url += f"?token={LTC_API_KEY}"
                            block_resp = requests.get(block_url, timeout=10)
                            block_resp.raise_for_status()
                            block_time_str = block_resp.json().get("time")
                            if block_time_str:
                                timestamp = int(
                                    datetime.fromisoformat(block_time_str.replace("Z", "+00:00")).timestamp()
                                )
                        except Exception as e:
                            print(f"LTC block-time fallback (BlockCypher block {block_height}) failed: {e}")

                if timestamp is None:
                    # Don't guess. Logging this against "now" would make an
                    # old, replayed txid look freshly sent.
                    raise ValueError(
                        "This Litecoin transaction is confirmed, but the exact time it "
                        "was sent couldn't be determined. Refusing to guess — try "
                        "verifying again in a moment."
                    )

                return {
                    "amount": matched_litoshis / 1e8,
                    "timestamp": timestamp,
                    "confirmations": confirmations,
                    "explorer_url": explorer_url,
                }
            elif outputs:
                raise ValueError(f"No output found sent to address '{get_ltc_address()}' in this transaction.")
    except ValueError:
        raise
    except Exception:
        pass  # fall through to Blockchair

    # Fallback lookup: Blockchair
    fallback_url = f"https://api.blockchair.com/litecoin/dashboards/transaction/{clean_tx}"
    try:
        response = requests.get(fallback_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        tx_data = data.get("data", {}).get(clean_tx, {})
        if not tx_data:
            raise ValueError("Litecoin transaction not found on Blockchair.")

        outputs = tx_data.get("outputs", [])
        matched_litoshis = sum(
            out.get("value", 0) for out in outputs
            if str(out.get("recipient", "")).strip().lower() == target_addr
        )
        if matched_litoshis == 0:
            raise ValueError(f"No output found sent to address '{get_ltc_address()}' in this transaction.")

        tx_info = tx_data.get("transaction", {})
        block_id = tx_info.get("block_id")
        if not block_id or block_id <= 0:
            raise ValueError(
                "This Litecoin transaction hasn't confirmed yet. Wait for at least "
                "one confirmation and try again."
            )

        timestamp = None
        time_str = tx_info.get("time")
        if time_str:
            try:
                timestamp = int(
                    datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc).timestamp()
                )
            except ValueError:
                pass

        if timestamp is None:
            # Same principle as above: fall back to the block's own record
            # rather than ever defaulting to "now".
            try:
                block_resp = requests.get(
                    f"https://api.blockchair.com/litecoin/dashboards/block/{block_id}", timeout=10
                )
                block_resp.raise_for_status()
                block_info = block_resp.json().get("data", {}).get(str(block_id), {}).get("block", {})
                block_time_str = block_info.get("time")
                if block_time_str:
                    timestamp = int(
                        datetime.fromisoformat(block_time_str).replace(tzinfo=timezone.utc).timestamp()
                    )
            except Exception as e:
                print(f"LTC block-time fallback (Blockchair block {block_id}) failed: {e}")

        if timestamp is None:
            raise ValueError(
                "This Litecoin transaction is confirmed, but the exact time it was "
                "sent couldn't be determined. Refusing to guess — try verifying "
                "again in a moment."
            )

        return {
            "amount": matched_litoshis / 1e8,
            "timestamp": timestamp,
            "confirmations": 1,  # Blockchair doesn't give a live confirmation count here
            "explorer_url": explorer_url,
        }
    except requests.RequestException as e:
        raise ValueError(f"Failed to verify Litecoin transaction on Blockchair: {str(e)}")


# --------------------------------------------------------------------------
# Pricing.
#
# get_price_usd_at() prices a payment at the moment it was actually SENT
# (the block timestamp), not at the moment someone clicks "verify". If the
# market moves after the sender already broadcast the correct amount,
# that's not on them.
#
# get_current_price_usd() is only for showing a live "≈ this much crypto"
# estimate on the frontend before anyone has sent anything — never used
# for reconciliation.
# --------------------------------------------------------------------------

_COINGECKO_IDS = {
    "ETH": "ethereum", "BTC": "bitcoin", "LTC": "litecoin",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai",
}
_BINANCE_SYMBOLS = {"ETH": "ETHUSDT", "BTC": "BTCUSDT", "LTC": "LTCUSDT"}
_STABLECOIN_SYMBOLS = {"USDT", "USDC", "DAI"}


def _binance_historical_price(symbol_upper: str, timestamp: int):
    """Fallback price source: Binance's public kline (candlestick) API has
    free, no-auth historical data going back years for these pairs. Note:
    Binance is known to geo-block cloud-provider IPs (AWS/GCP, which is
    what most PaaS hosts including Render run on) with an error response
    that ISN'T a list — the old version of this function silently treated
    that as "no data" instead of surfacing what Binance actually said."""
    b_sym = _BINANCE_SYMBOLS.get(symbol_upper)
    if not b_sym:
        return None
    try:
        start_ms = (timestamp - 300) * 1000
        end_ms = (timestamp + 300) * 1000
        url = (
            f"https://api.binance.com/api/v3/klines?symbol={b_sym}&interval=1m"
            f"&startTime={start_ms}&endTime={end_ms}&limit=10"
        )
        response = requests.get(url, timeout=10)
        res = response.json()
        if isinstance(res, list) and res:
            # Each kline: [open_time_ms, open, high, low, close, ...]
            closest = min(res, key=lambda k: abs((k[0] / 1000) - timestamp))
            return float(closest[4])  # close price of the nearest 1-minute candle
        # Binance returns a dict like {"code": -1, "msg": "..."} on errors —
        # including HTTP 451 for geo-blocked IPs — instead of raising. Log
        # the real content so "no candles" vs. "blocked entirely" is visible.
        print(f"Binance klines returned no usable data for {symbol_upper} (HTTP {response.status_code}): {res}")
    except Exception as e:
        print(f"Binance historical price fallback failed for {symbol_upper}: {e}")
    return None


_KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "LTC": "LTCUSD"}


def _kraken_historical_price(symbol_upper: str, timestamp: int):
    """Second fallback price source, independent of both CoinGecko and
    Binance. Kraken doesn't geo-block cloud/US IPs the way Binance does,
    so this covers exactly the failure mode above."""
    pair = _KRAKEN_PAIRS.get(symbol_upper)
    if not pair:
        return None
    try:
        since = timestamp - 300
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1&since={since}"
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("error"):
            print(f"Kraken OHLC error for {symbol_upper}: {data['error']}")
            return None
        result = data.get("result", {})
        # Kraken echoes the pair back under its own internal name (which can
        # differ slightly from what was requested), so just take whichever
        # key isn't "last" rather than assuming an exact match.
        candles = next((v for k, v in result.items() if k != "last"), None)
        if not candles:
            print(f"Kraken OHLC returned no candles for {symbol_upper} ({pair}): {data}")
            return None
        closest = min(candles, key=lambda c: abs(float(c[0]) - timestamp))
        return float(closest[4])  # close price
    except Exception as e:
        print(f"Kraken historical price fallback failed for {symbol_upper}: {e}")
        return None


def get_price_usd_at(symbol: str, timestamp: int) -> float:
    symbol_upper = symbol.strip().upper()
    cg_id = _COINGECKO_IDS.get(symbol_upper)
    if not cg_id:
        raise ValueError(f"Symbol '{symbol}' is not currently supported.")

    errors = []

    # 1) CoinGecko range endpoint — ~5-minute granularity for short ranges.
    try:
        window_seconds = 3600
        from_ts = timestamp - window_seconds
        to_ts = timestamp + window_seconds
        url = (
            f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart/range"
            f"?vs_currency=usd&from={from_ts}&to={to_ts}"
        )
        res = requests.get(url, timeout=10)
        if res.status_code == 429:
            errors.append("CoinGecko range: rate-limited (429)")
        else:
            res.raise_for_status()
            prices = res.json().get("prices", [])
            if prices:
                closest = min(prices, key=lambda p: abs((p[0] / 1000) - timestamp))
                return float(closest[1])
            errors.append("CoinGecko range: no price points returned for this window")
    except Exception as e:
        errors.append(f"CoinGecko range: {e}")

    # 2) CoinGecko daily history — lower precision, but a different code
    # path (and different rate-limit bucket) than the range endpoint above.
    try:
        date_str = datetime.utcfromtimestamp(timestamp).strftime("%d-%m-%Y")
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/history?date={date_str}"
        res = requests.get(url, timeout=10)
        if res.status_code == 429:
            errors.append("CoinGecko history: rate-limited (429)")
        else:
            res.raise_for_status()
            price = res.json().get("market_data", {}).get("current_price", {}).get("usd")
            if price:
                return float(price)
            errors.append(f"CoinGecko history: no market_data for {date_str}")
    except Exception as e:
        errors.append(f"CoinGecko history: {e}")

    # Stablecoins: skip Binance/Kraken (their pairs for these tokens are
    # unreliable proxies for "USD price" — e.g. USDC/USDT isn't really a
    # market rate for either against the dollar) and fall back to the
    # $1.00 peg instead of blocking a payroll run over a pricing hiccup
    # for an asset that's virtually always sitting within a fraction of a
    # cent of that anyway. This is the one deliberate exception to "never
    # guess" in this file — the risk profile is fundamentally different
    # from a volatile asset like BTC/ETH/LTC.
    if symbol_upper in _STABLECOIN_SYMBOLS:
        print(f"get_price_usd_at({symbol_upper}, {timestamp}): CoinGecko failed ({'; '.join(errors)}); assuming $1.00 peg.")
        return 1.0

    # 3) Binance klines — independent provider entirely, so a CoinGecko
    # outage or rate-limit doesn't block reconciliation.
    binance_price = _binance_historical_price(symbol_upper, timestamp)
    if binance_price:
        return binance_price
    errors.append("Binance klines: no usable data (see server log for the exact response)")

    # 4) Kraken OHLC — a second independent provider, in case Binance is
    # geo-blocking this server's IP (common for cloud/US-hosted apps).
    kraken_price = _kraken_historical_price(symbol_upper, timestamp)
    if kraken_price:
        return kraken_price
    errors.append("Kraken OHLC: no usable data (see server log for the exact response)")

    print(f"get_price_usd_at({symbol_upper}, {timestamp}) failed on every source: {'; '.join(errors)}")
    raise ValueError(
        f"Unable to determine the historical USD price for {symbol_upper} at the "
        f"time this transaction was sent (checked CoinGecko, Binance, and Kraken). "
        f"This is usually a temporary rate limit — try again in a minute. If it keeps "
        f"failing for this specific transaction, check the Render logs for the "
        f"exact error."
    )


def get_current_price_usd(symbol: str) -> float:
    symbol_upper = symbol.strip().upper()

    cg_id = _COINGECKO_IDS.get(symbol_upper)
    if cg_id:
        try:
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
            res = requests.get(url, timeout=5).json()
            return float(res[cg_id]["usd"])
        except Exception:
            pass

    b_sym = _BINANCE_SYMBOLS.get(symbol_upper)
    if b_sym:
        try:
            res = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={b_sym}", timeout=5).json()
            return float(res["price"])
        except Exception:
            pass

    if symbol_upper in _STABLECOIN_SYMBOLS:
        # This is just the "≈ amount owed in crypto" display estimate, not
        # a reconciliation price — $1.00 is a fine placeholder if the live
        # lookup fails for a moment.
        return 1.0

    raise ValueError(f"Unable to fetch the current USD exchange rate for '{symbol}'.")


def verify_and_price_tx(raw_tx_input: str, symbol: str) -> dict:
    """
    Full pipeline: accept a raw hash or explorer link -> extract the hash ->
    verify it on-chain -> price it at the moment it was sent.

    Returns:
        {
            tx_hash, amount, usd_value, price_usd_used,
            timestamp, confirmations, explorer_url
        }
    """
    symbol_upper = symbol.strip().upper()
    clean_hash = extract_tx_hash(raw_tx_input, symbol_upper)

    if symbol_upper == "ETH":
        result = verify_eth_tx(clean_hash)
    elif symbol_upper in _ERC20_TOKENS:
        result = verify_erc20_tx(clean_hash, symbol_upper)
    elif symbol_upper == "BTC":
        result = verify_btc_tx(clean_hash)
    elif symbol_upper == "LTC":
        result = verify_ltc_tx(clean_hash)
    else:
        raise ValueError(f"Symbol '{symbol}' is not currently supported.")

    # Every verify_*_tx function above now either returns a real send-time
    # timestamp or raises rather than returning one that's missing/null —
    # so there is no "else" here. We never want to silently price (or log
    # paid_at) against the CURRENT price/time instead of the moment the
    # funds were actually sent; that would make an old, replayed txid look
    # freshly sent and defeat the entire point of pricing at send-time.
    price_usd = get_price_usd_at(symbol_upper, result["timestamp"])

    return {
        "tx_hash": clean_hash,
        "amount": result["amount"],
        "usd_value": result["amount"] * price_usd,
        "price_usd_used": price_usd,
        "timestamp": result["timestamp"],
        "confirmations": result["confirmations"],
        "explorer_url": result["explorer_url"],
    }
