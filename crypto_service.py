import os
import requests

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

def verify_eth_tx(tx_id: str):
    """
    Verifies an Ethereum transaction hash via Etherscan API.
    Returns (amount_in_eth, timestamp) or raises ValueError on failure.
    """
    clean_tx = tx_id.strip()
    if not clean_tx or not clean_tx.startswith("0x") or len(clean_tx) != 66:
        raise ValueError("Invalid Ethereum transaction hash format.")

    url = f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={clean_tx}&apikey={ETHERSCAN_API_KEY}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise ValueError(f"Failed to communicate with Etherscan API: {str(e)}")

    result = data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError("Transaction not found or invalid Etherscan response.")

    # Convert hex value in Wei to ETH
    value_wei_hex = result.get("value", "0x0")
    try:
        value_wei = int(value_wei_hex, 16)
        amount_eth = value_wei / 1e18
    except ValueError:
        raise ValueError("Failed to parse transaction transfer value.")

    # Get block timestamp (optional / fallback to current if unconfirmed)
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

def get_eth_price_usd() -> float:
    """Fetches real-time ETH price in USD from CoinGecko or fallback API."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
        res = requests.get(url, timeout=5).json()
        return float(res["ethereum"]["usd"])
    except Exception:
        # Fallback pricing endpoint
        try:
            res = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=5).json()
            return float(res["price"])
        except Exception:
            raise ValueError("Unable to fetch current ETH/USD price from exchange APIs.")

def get_tx_usd_value(tx_id: str, symbol: str) -> float:
    """Calculates total USD value of a crypto transaction."""
    symbol_upper = symbol.strip().upper()
    
    if symbol_upper == "ETH":
        amount_eth, _ = verify_eth_tx(tx_id)
        eth_price = get_eth_price_usd()
        return amount_eth * eth_price
    else:
        raise ValueError(f"Symbol '{symbol}' is not currently supported for automatic verification.")
