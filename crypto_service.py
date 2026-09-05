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

    if symbol_upper == "ETH":
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


def verify_eth_tx(tx_hash: str) -> dict:
    """Verifies a native ETH transfer via Etherscan's API V2 (mainnet,
    chainid=1). ERC-20 transfers are rejected — their decimals and USD
    pricing can't be safely inferred generically, so token payments should
    be reconciled manually."""
    target_addr = get_eth_address().lower()
    clean_tx = tx_hash.strip().lower()
    if not _ETH_HASH_RE.fullmatch(clean_tx):
        raise ValueError("Invalid Ethereum transaction hash format (must be 0x + 64 hex characters).")

    api_key = _require_etherscan_key()
    # Etherscan fully deprecated the old V1 base URL (api.etherscan.io/api)
    # on 2025-08-15. V2 requires a chainid query param; 1 = Ethereum mainnet.
    base = "https://api.etherscan.io/v2/api"
    chainid = 1

    tx_data = _eth_rpc(
        f"{base}?chainid={chainid}&module=proxy&action=eth_getTransactionByHash&txhash={clean_tx}&apikey={api_key}"
    )
    result = tx_data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError("Ethereum transaction not found. Verify the TxID on Etherscan.")

    # A transaction can be mined but still have reverted (spent gas, moved
    # nothing). eth_getTransactionByHash alone can't tell you that — you
    # need the receipt's status field.
    #
    # This function makes up to 4 sequential Etherscan calls total. A short
    # pause between them keeps a single verification comfortably under a
    # free-tier key's 3-req/sec cap, instead of relying only on _eth_rpc's
    # reactive retry for a limit that a fresh key can trip on its very
    # first use.
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
    elif input_data.startswith("0xa9059cbb"):
        raise ValueError(
            "This looks like an ERC-20 token transfer, not native ETH. Token "
            "payments aren't auto-verified (decimals and pricing vary by token) — "
            "please reconcile this one manually."
        )
    else:
        raise ValueError(f"Transaction recipient ('{tx_to}') does not match the designated wallet '{get_eth_address()}'.")

    if amount_eth <= 0:
        raise ValueError("Transaction contains 0 ETH transferred to the target address.")

    block_number_hex = result.get("blockNumber")
    timestamp = None
    confirmations = 0
    if block_number_hex:
        time.sleep(0.35)
        block_data = _eth_rpc(
            f"{base}?chainid={chainid}&module=proxy&action=eth_getBlockByNumber&tag={block_number_hex}&boolean=false&apikey={api_key}"
        )
        block_result = block_data.get("result") or {}
        if "timestamp" in block_result:
            timestamp = int(block_result["timestamp"], 16)

        time.sleep(0.35)
        latest_data = _eth_rpc(f"{base}?chainid={chainid}&module=proxy&action=eth_blockNumber&apikey={api_key}")
        latest_hex = latest_data.get("result")
        if latest_hex:
            confirmations = max(0, int(latest_hex, 16) - int(block_number_hex, 16) + 1)

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


def verify_btc_tx(tx_hash: str) -> dict:
    """Verifies a Bitcoin transaction via the Blockstream Esplora API."""
    target_addr = get_btc_address().lower()
    clean_tx = tx_hash.strip().lower()
    if not _HEX64_RE.fullmatch(clean_tx):
        raise ValueError("Invalid Bitcoin transaction hash (must be 64 hexadecimal characters).")

    url = f"https://blockstream.info/api/tx/{clean_tx}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            raise ValueError("Bitcoin transaction not found.")
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise ValueError(f"Failed to communicate with the Bitcoin blockchain API: {str(e)}")

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
        # Blockstream's tx status almost always includes block_time, but if
        # it's ever missing, the block endpoint always has "timestamp".
        try:
            block_resp = requests.get(f"https://blockstream.info/api/block/{block_hash}", timeout=10)
            block_resp.raise_for_status()
            timestamp = block_resp.json().get("timestamp")
        except Exception as e:
            print(f"BTC block-time fallback failed for block {block_hash}: {e}")

    if block_height:
        try:
            tip_resp = requests.get("https://blockstream.info/api/blocks/tip/height", timeout=10)
            tip_resp.raise_for_status()
            confirmations = max(0, int(tip_resp.text) - block_height + 1)
        except (requests.RequestException, ValueError):
            confirmations = 1

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

_COINGECKO_IDS = {"ETH": "ethereum", "BTC": "bitcoin", "LTC": "litecoin"}
_BINANCE_SYMBOLS = {"ETH": "ETHUSDT", "BTC": "BTCUSDT", "LTC": "LTCUSDT"}


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
