import os
import requests

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
LTC_API_KEY = os.getenv("LTC_API_KEY", os.getenv("BLOCKCYPHER_API_KEY", ""))

# Designated Receiving Wallet Addresses
BTC_RECEIVING_ADDRESS = os.getenv("BTC_RECEIVING_ADDRESS", "bc1qq2xjkur4jkn76g3v7hwtx94r2733l8hr9yfdem").strip()
LTC_RECEIVING_ADDRESS = os.getenv("LTC_RECEIVING_ADDRESS", "LSzh8EETPhDd7xMaxVu9gYZ8Sa3ucwVphg").strip()
ETH_RECEIVING_ADDRESS = os.getenv("ETH_RECEIVING_ADDRESS", "0x67803EfDf6EfBcE405275F42242f5f617FAf9194").strip()

def verify_eth_tx(tx_id: str):
    """
    Verifies an Ethereum transaction hash via Etherscan API.
    Supports direct native ETH transfers and standard ERC-20 token transfers.
    Calculates transferred value sent specifically to the designated target address.
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

    if "error" in data:
        raise ValueError(f"Etherscan API Error: {data['error'].get('message', 'Unknown API error')}")

    result = data.get("result")
    if not result or not isinstance(result, dict):
        raise ValueError("Ethereum transaction not found. Verify the TxID on Etherscan.")

    target_addr = ETH_RECEIVING_ADDRESS.lower()
    tx_to = str(result.get("to") or "").strip().lower()
    input_data = str(result.get("input") or "0x")

    amount_eth = 0.0

    # Case 1: Direct Native ETH Transfer
    if tx_to == target_addr:
        value_wei_hex = result.get("value", "0x0")
        try:
            value_wei = int(value_wei_hex, 16)
            amount_eth = value_wei / 1e18
        except ValueError:
            raise ValueError("Failed to parse Ethereum transaction transfer value.")

    # Case 2: ERC-20 Token Transfer (method signature 0xa9059cbb = transfer(address,uint256))
    elif input_data.startswith("0xa9059cbb") and len(input_data) >= 138:
        recipient_hex = "0x" + input_data[34:74].lower()
        if recipient_hex != target_addr:
            raise ValueError(f"ERC-20 transfer target ('{recipient_hex}') does not match designated wallet '{ETH_RECEIVING_ADDRESS}'.")

        value_hex = "0x" + input_data[74:138]
        try:
            raw_value = int(value_hex, 16)
            amount_eth = raw_value / 1e18
        except ValueError:
            raise ValueError("Failed to parse ERC-20 transfer token value.")

    else:
        raise ValueError(f"Transaction recipient ('{tx_to}') does not match target address '{ETH_RECEIVING_ADDRESS}'.")

    if amount_eth <= 0:
        raise ValueError("Transaction contains 0 transferred value to the target address.")

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
    Inspects transaction vouts and sums only Satoshis sent to target receiving address.
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

    target_addr = BTC_RECEIVING_ADDRESS.lower()
    matched_satoshis = sum(
        vout.get("value", 0) for vout in vouts
        if str(vout.get("scriptpubkey_address", "")).strip().lower() == target_addr
    )

    if matched_satoshis == 0:
        raise ValueError(f"No outputs found sent to target address '{BTC_RECEIVING_ADDRESS}' in this transaction.")

    amount_btc = matched_satoshis / 1e8
    timestamp = data.get("status", {}).get("block_time")
    return amount_btc, timestamp

def verify_ltc_tx(tx_id: str):
    """
    Verifies a Litecoin (LTC) transaction hash.
    Inspects outputs and sums only Litoshis sent to target receiving address.
    """
    clean_tx = tx_id.strip()
    if not clean_tx or len(clean_tx) != 64:
        raise ValueError("Invalid Litecoin transaction hash (must be 64 hexadecimal characters).")

    target_addr = LTC_RECEIVING_ADDRESS.lower()

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
                return matched_litoshis / 1e8, None
            elif outputs:
                raise ValueError(f"No output found sent to address '{LTC_RECEIVING_ADDRESS}' in this transaction.")
    except ValueError as ve:
        raise ve
    except Exception:
        pass

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
            raise ValueError(f"No output found sent to address '{LTC_RECEIVING_ADDRESS}' in this transaction.")

        return matched_litoshis / 1e8, None
    except requests.RequestException as e:
        raise ValueError(f"Failed to verify Litecoin transaction on Blockchair: {str(e)}")

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
