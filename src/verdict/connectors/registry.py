from verdict.connectors.binance_us import BinanceUSConnector
from verdict.connectors.binance_us import normalize_trades as normalize_binance_us
from verdict.connectors.coinbase import CoinbaseConnector, normalize_ticker
from verdict.connectors.kraken import KrakenConnector
from verdict.connectors.kraken import normalize_trades as normalize_kraken

CONNECTORS = {
    "coinbase": CoinbaseConnector,
    "kraken": KrakenConnector,
    "binance_us": BinanceUSConnector,
}
NORMALIZERS = {
    "coinbase": normalize_ticker,
    "kraken": normalize_kraken,
    "binance_us": normalize_binance_us,
}
