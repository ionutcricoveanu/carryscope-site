# CarryScope (web)

The CarryScope site: a free, weekly-dated **"is BTC/ETH carry worth it right now?"** check. It shows the
gross funding APY the dashboards quote, the **net-of-cost** number after fees and the basis spread, and a
verdict versus the risk-free rate. Analytics only, not financial advice.

Static site, deploys to Cloudflare Pages.

| File | What |
|------|------|
| `index.html` | The live tool (generated). |
| `methodology.html` | How the number is computed. |
| `index.template.html` | Page template (edit copy here, then rebuild). |
| `build.py` | Recomputes BTC/ETH net-of-cost carry from Binance's public data dumps and rebuilds the page. |
| `carry-data.json` | The current numbers (generated). |

Rebuild: `python3 build.py` (no API key, standard library only). Automated weekly by
`.github/workflows/refresh.yml`.

Data comes from Binance's public dump CDN (`data.binance.vision`) plus the official market-data-only
spot mirror (`data-api.binance.vision`), not the trading API: the trading API geo-blocks US IPs, where
GitHub-hosted runners live (HTTP 451). Funding newer than the last monthly dump is reconstructed from
premium-index data with Binance's published formula, validated in-build against the last settled month
(the build fails rather than publish a drifting number).

The open, standalone method script: https://github.com/ionutcricoveanu/carryscope-methodology

Crypto carry involves real risk including liquidation and loss. Figures are a dated illustration and may
be delayed or wrong; verify before trading.
