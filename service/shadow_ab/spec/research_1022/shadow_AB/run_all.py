"""Daily routine: refresh public data once, then recompute ledgers A, B, C, D, E. Run after 00:05 UTC (08:05 Asia/Taipei)."""
import subprocess, sys, os
d = os.path.dirname(os.path.abspath(__file__)); py = sys.executable
subprocess.run([py, "ledger_c.py"], cwd=d, check=True)                       # fetch + C
for f in ("ledger_a.py", "ledger_b.py", "ledger_e.py"): subprocess.run([py, f, "--no-fetch"], cwd=d, check=True)
subprocess.run([py, "ledger_d.py"], cwd=d, check=True)                       # D fetches its own BTC/ETH data (small)
