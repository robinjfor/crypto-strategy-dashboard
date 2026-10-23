"""Ledger A: 0.7 U1 tv0.40 / rest S5 long/short, leverage x1.0, same rules/breakers as ledger C. Start 2026-10-23, 1000 USDC, to 2026-11-20.
Usage: python3 ledger_a.py [--no-fetch]"""
import os, runpy, sys
os.environ.update(LEDGER_NAME="A", LEDGER_W="0.7", LEDGER_LV="1.0")
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger_c.py"), run_name="__main__")
