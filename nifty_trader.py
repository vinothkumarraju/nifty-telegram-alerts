name: Nifty Live Strategy Runner

on:
  schedule:
    # Runs every 5 minutes during Indian Market Hours (09:05 AM to 03:35 PM IST / Mon-Fri 03:35 to 10:05 UTC)
    - cron: '*/5 3-10 * * 1-5'
  workflow_dispatch: # Allows manual trigger from GitHub UI

permissions:
  contents: write

jobs:
  run-trader:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install Required Packages
        run: |
          python -m pip install --upgrade pip
          pip install pandas numpy requests

      - name: Execute Strategy Engine
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
        run: |
          python nifty_trader.py

      - name: Persist State & Trade Logs to Repo
        if: always()
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git add strategy_state.json paper_trades.csv || true
          git commit -m "Auto-update strategy state & logs [skip ci]" || echo "No state changes to commit"
          git push || true
