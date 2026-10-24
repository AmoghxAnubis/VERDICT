export const labels: Record<string, string> = {
  coinbase: "Coinbase", kraken: "Kraken", binance_us: "Binance.US", chainlink: "Chainlink", pyth: "Pyth", uniswap_v3: "Uniswap v3",
  baseline: "Healthy agreement", single_spike: "Single-source spike", market_move: "Market-wide move",
  correlated_pair: "Correlated pair", correlated_majority: "Correlated majority", source_outage: "Source outage",
  shared_outage: "Shared dependency outage", late_receipt: "Delayed receipts",
};
export const human = (s: string) => labels[s] || s.replaceAll("_", " ").replaceAll(":", " · ");
export const short = (s: string | null | undefined) => s ? `${s.slice(0, 7)}…${s.slice(-5)}` : "—";
// Decimal text remains exact. Numeric conversions elsewhere are only for chart geometry.
export function price(s: string | null | undefined) {
  if (!s) return "—";
  const [integer, fraction] = s.split(".");
  return "$" + integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? `.${fraction}` : ".00");
}
export const percent = (n: number | null | undefined) => n == null ? "—" : `${(n / 100).toFixed(2)}%`;
export const time = (s: string) => new Date(s).toLocaleTimeString("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const fullTime = (s: string) => new Date(s).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC";
