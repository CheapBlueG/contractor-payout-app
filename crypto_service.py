import os
import requests
from datetime import datetime, timezone

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

def get_coingecko_historical_price(coin_id: str, timestamp: int) -> float:
    """Fetch historical USD price for a coin around a specific UNIX timestamp."""
    from_ts = timestamp - 1800
    to_ts = timestamp + 1800
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
    params = {"vs_currency": "usd", "from": from_ts, "to": to_ts}
    
    res = requests.get(url, params=params, timeout=10)
    if res.status_code == 200:
        prices = res.json().get("prices", [])
        if prices:
            return float(prices[0][1])
    return 0.0

def verify_eth_tx(tx_hash: str) -> tuple[float, int]:
    """Returns (ETH_amount, timestamp)"""
    url = f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={tx_hash}&apikey={ETHERSCAN_API_KEY}"
    res = requests.get(url, timeout=10).json()
    result = res.get("result")
    if not result:
        return 0.0, 0
    
    value_wei = int(result.get("value", "0x0"), 16)
    eth_amount = value_wei / 1e18
    block_num = result.get("blockNumber")
    
    timestamp = int(datetime.now(timezone.utc).timestamp())
    if block_num:
        block_url = f"https://api.etherscan.io/api?module=proxy&action=eth_getBlockByNumber&tag={block_num}&boolean=false&apikey={ETHERSCAN_API_KEY}"
        block_res = requests.get(block_url, timeout=10).json()
        if block_res.get("result"):
            timestamp = int(block_res["result"].get("timeStamp", timestamp), 16)
            
    return eth_amount, timestamp

def verify_btc_tx(tx_hash: str) -> tuple[float, int]:
    """Returns (BTC_amount, timestamp) via Blockstream"""
    url = f"https://blockstream.info/api/tx/{tx_hash}"
    res = requests.get(url, timeout=10)
    if res.status_code != 200:
        return 0.0, 0
    data = res.json()
    satoshis = sum(out.get("value", 0) for out in data.get("vout", []))
    btc_amount = satoshis / 1e8
    timestamp = data.get("status", {}).get("block_time", int(datetime.now(timezone.utc).timestamp()))
    return btc_amount, timestamp

def verify_ltc_tx(tx_hash: str) -> tuple[float, int]:
    """Returns (LTC_amount, timestamp) via Blockcypher"""
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}"
    res = requests.get(url, timeout=10)
    if res.status_code != 200:
        return 0.0, 0
    data = res.json()
    ltc_amount = data.get("total", 0) / 1e8
    confirmed_str = data.get("confirmed")
    if confirmed_str:
        dt = datetime.strptime(confirmed_str.split(".")[0], "%Y-%m-%dT%H:%M:%SZ")
        timestamp = int(dt.replace(tzinfo=timezone.utc).timestamp())
    else:
        timestamp = int(datetime.now(timezone.utc).timestamp())
    return ltc_amount, timestamp

def get_tx_usd_value(tx_id: str, symbol: str) -> float:
    symbol = symbol.upper()
    amount, timestamp = 0.0, 0
    coin_id = ""

    if symbol == "ETH":
        amount, timestamp = verify_eth_tx(tx_id)
        coin_id = "ethereum"
    elif symbol == "BTC":
        amount, timestamp = verify_btc_tx(tx_id)
        coin_id = "bitcoin"
    elif symbol == "LTC":
        amount, timestamp = verify_ltc_tx(tx_id)
        coin_id = "litecoin"

    if amount > 0 and timestamp > 0:
        price_usd = get_coingecko_historical_price(coin_id, timestamp)
        return amount * price_usd
    return 0.0
