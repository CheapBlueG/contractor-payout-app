import os
import requests

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

def verify_eth_tx(tx_id: str):
    """
    Verifies an Ethereum transaction hash via Etherscan API.
    Returns (amount_eth, timestamp).
    """
    clean_tx = tx_id.strip()
    if not clean_tx or not clean_tx.startswith("0x") or len(clean_tx) != 66:
        raise ValueError("Invalid Ethereum transaction hash format (must start with 0x and be 66 characters long).")

    url = f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={clean_tx}&apikey={ETHERSCAN_API_KEY}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise ValueError(f"Failed to communicate with Etherscan API: {str(e)}")

    result = data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError("Ethereum transaction not found or invalid response.")

    value_wei_hex = result.get("value", "0x0")
    try:
        value_wei = int(value_wei_hex, 16)
        amount_eth = value_wei / 1e18
    except ValueError:
        raise ValueError("Failed to parse Ethereum transaction transfer value.")

    timestamp = None
    block_number = result.get("blockNumber")
    if block_number:
        try:
            block_url = f"https://api.etherscan.io/api?module=proxy&action=eth_getBlockByNumber&tag={block_number}&boolean=false&apikey={ETHERSCAN_API_KEY}"
            block_resp = requests.get(block_url, timeout=10).json()
            block_result = block_resp.get("result", {})
            if block_result and "timestamp" in block_result:
                timestamp = int(block_result["timestamp"], 16)
        except Exception:
            pass

    return amount_eth, timestamp

def verify_btc_tx(tx_id: str):
    """
    Verifies a Bitcoin (BTC) transaction hash via Blockstream Esplora API.
    Returns (amount_btc, timestamp).
    """
    clean_tx = tx_id.strip()
    if not clean_tx or len(clean_tx) != 64:
        raise ValueError("Invalid Bitcoin transaction hash (must be 64 hexadecimal characters).")

    url = f"https://blockstream.info/api/tx/{clean_tx}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            raise ValueError("Bitcoin transaction not found.")
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise ValueError(f"Failed to communicate with Bitcoin blockchain API: {str(e)}")

    vouts = data.get("vout", [])
    if not vouts:
        raise ValueError("No output amounts found in Bitcoin transaction.")

    # Sum total output value in Satoshis (1 BTC = 100,000,000 Satoshis)
    total_satoshis = sum(vout.get("value", 0) for vout in vouts)
    amount_btc = total_satoshis / 1e8

    timestamp = data.get("status", {}).get("block_time")
    return amount_btc, timestamp

def verify_ltc_tx(tx_id: str):
    """
    Verifies a Litecoin (LTC) transaction hash via BlockCypher / Blockchair API.
    Returns (amount_ltc, timestamp).
    """
    clean_tx = tx_id.strip()
    if not clean_tx or len(clean_tx) != 64:
        raise ValueError("Invalid Litecoin transaction hash (must be 64 hexadecimal characters).")

    # Primary lookup via BlockCypher API
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{clean_tx}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            total_litoshis = data.get("total", 0)
            if not total_litoshis and "outputs" in data:
                total_litoshis = sum(out.get("value", 0) for out in data.get("outputs", []))
            amount_ltc = total_litoshis / 1e8
            return amount_ltc, None
    except Exception:
        pass

    # Fallback lookup via Blockchair API
    fallback_url = f"https://api.blockchair.com/litecoin/dashboards/transaction/{clean_tx}"
    try:
        response = requests.get(fallback_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        tx_data = data.get("data", {}).get(clean_tx, {})
        if not tx_data:
            raise ValueError("Litecoin transaction not found.")
        
        total_litoshis = tx_data.get("transaction", {}).get("output_total", 0)
        amount_ltc = total_litoshis / 1e8
        return amount_ltc, None
    except requests.RequestException as e:
        raise ValueError(f"Failed to verify Litecoin transaction: {str(e)}")

def get_crypto_price_usd(symbol: str) -> float:
    """Fetches real-time crypto price in USD (ETH, BTC, LTC)."""
    symbol_upper = symbol.strip().upper()
    
    coingecko_ids = {
        "ETH": "ethereum",
        "BTC": "bitcoin",
        "LTC": "litecoin"
    }

    cg_id = coingecko_ids.get(symbol_upper)
    if cg_id:
        try:
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
            res = requests.get(url, timeout=5).json()
            return float(res[cg_id]["usd"])
        except Exception:
            pass

    # Fallback to Binance Ticker API
    binance_symbols = {
        "ETH": "ETHUSDT",
        "BTC": "BTCUSDT",
        "LTC": "LTCUSDT"
    }
    b_sym = binance_symbols.get(symbol_upper)
    if b_sym:
        try:
            res = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={b_sym}", timeout=5).json()
            return float(res["price"])
        except Exception:
            pass

    raise ValueError(f"Unable to fetch current USD exchange rate for '{symbol}'.")

def get_tx_usd_value(tx_id: str, symbol: str) -> float:
    """Calculates total USD value of a crypto transaction (ETH, BTC, LTC)."""
    symbol_upper = symbol.strip().upper()
    
    if symbol_upper == "ETH":
        amount, _ = verify_eth_tx(tx_id)
    elif symbol_upper == "BTC":
        amount, _ = verify_btc_tx(tx_id)
    elif symbol_upper == "LTC":
        amount, _ = verify_ltc_tx(tx_id)
    else:
        raise ValueError(f"Symbol '{symbol}' is not currently supported.")

    price_usd = get_crypto_price_usd(symbol_upper)
    return amount * price_usd
