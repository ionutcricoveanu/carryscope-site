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
| `build.py` | Recomputes BTC/ETH net-of-cost carry from public Binance data and rebuilds the page. |
| `carry-data.json` | The current numbers (generated). |

Rebuild: `python3 build.py` (no API key, standard library only). Automated weekly by
`.github/workflows/refresh.yml`.

The open, standalone method script: https://github.com/ionutcricoveanu/carryscope-methodology

Crypto carry involves real risk including liquidation and loss. Figures are a dated illustration and may
be delayed or wrong; verify before trading.
