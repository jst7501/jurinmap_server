# stocks_parts layout

`server/routes/stocks.py` loads these files in a single shared namespace.
Order matters for globals used across parts.

Current load order:

1. `part01_realtime_base.py`: common imports, shared state, realtime helpers
2. `part02_list_theme_market.py`: list/theme/market snapshot APIs
3. `part03_search_news_live.py`: search/news/live WS APIs
4. `part04_ohlcv_rankings_basic.py`: ohlcv/ranking/basic market APIs
5. `part05_strength_timeline.py`: strength/timeline APIs
6. `part06_surge_investor_program.py`: surge/investor/program APIs
7. `part07_limitup_pollers.py`: pollers and background refresh jobs
8. `part08_home_ws_prewarm.py`: home websocket + prewarm runtime

When splitting or moving code:

- Keep shared globals in `part01` unless there is a strong reason otherwise.
- If a later part references symbols from another part, keep order explicit.
- Prefer adding helper functions over adding new cross-part globals.
