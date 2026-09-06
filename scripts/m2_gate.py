import sys
from refstudio.cli import main

print("Web smoke: pytest tests/test_web_smoke.py", flush=True)
sys.exit(main(["gate-m2", "--out", "out/m2", "--n", "20"]))
