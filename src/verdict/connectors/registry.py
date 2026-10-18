import httpx

from verdict.config import Settings
from verdict.connectors.binance_us import BinanceUSConnector
from verdict.connectors.binance_us import normalize_trades as normalize_binance_us
from verdict.connectors.chainlink import ChainlinkConnector, normalize_feed
from verdict.connectors.coinbase import CoinbaseConnector, normalize_ticker
from verdict.connectors.kraken import KrakenConnector
from verdict.connectors.kraken import normalize_trades as normalize_kraken
from verdict.connectors.pyth import PythConnector, normalize_price
from verdict.connectors.uniswap import UniswapConnector, normalize_pool
from verdict.models import Source

CONNECTORS = {
    "coinbase": CoinbaseConnector,
    "kraken": KrakenConnector,
    "binance_us": BinanceUSConnector,
    "chainlink": ChainlinkConnector,
    "pyth": PythConnector,
    "uniswap_v3": UniswapConnector,
}
NORMALIZERS = {
    "coinbase": normalize_ticker,
    "kraken": normalize_kraken,
    "binance_us": normalize_binance_us,
    "chainlink": normalize_feed,
    "pyth": normalize_price,
    "uniswap_v3": normalize_pool,
}


def build_connector(source: Source, client: httpx.AsyncClient, settings: Settings):
    options = {}
    if source in ("chainlink", "uniswap_v3"):
        options.update(rpc_url=settings.ethereum_rpc_url, block_tag=settings.ethereum_block_tag)
    if source == "uniswap_v3":
        options["conversion_max_age_seconds"] = settings.usdc_conversion_max_age_seconds
    if source == "pyth":
        options.update(
            hermes_url=settings.pyth_hermes_url,
            api_key=settings.pyth_api_key.get_secret_value(),
        )
    return CONNECTORS[source](client, settings.max_future_skew_seconds, **options)


def poll_interval(source: Source, settings: Settings) -> float:
    if source in ("chainlink", "uniswap_v3"):
        return settings.onchain_poll_interval_seconds
    return settings.poll_interval_seconds
