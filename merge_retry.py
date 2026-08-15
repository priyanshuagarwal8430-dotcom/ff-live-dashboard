"""
FF — Merge retry-pass results into fundamentals_master.csv
================================================================
Small helper used by the Weekly Fundamentals Check workflow. Kept as a
standalone script (instead of inline Python inside the workflow YAML) to
avoid YAML indentation issues with embedded code blocks.
"""

import os
import pandas as pd

if os.path.exists("fundamentals_retry.csv"):
    main_df = pd.read_csv("fundamentals_master.csv")
    retry_df = pd.read_csv("fundamentals_retry.csv")
    combined = pd.concat([main_df, retry_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["symbol", "quarter_label"], keep="last")
    combined.to_csv("fundamentals_master.csv", index=False)
    print(f"Recovered {retry_df['symbol'].nunique()} tickers on retry.")
else:
    print("No retry file found — nothing to merge.")
