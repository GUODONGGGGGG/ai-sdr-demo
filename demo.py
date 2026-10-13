"""Run a fixed sample: python3 demo.py A (or B/C)."""
import argparse
import json

from server.core import run_scenario

parser = argparse.ArgumentParser(description="AI SDR 第一阶段：预设样例模拟运行")
parser.add_argument("scenario", choices=["A", "B", "C"], help="演示场景")
args = parser.parse_args()
print(json.dumps(run_scenario(args.scenario), ensure_ascii=False, indent=2))
