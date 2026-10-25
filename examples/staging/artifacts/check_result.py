"""Small executable example; run from an extracted delivery root."""
from pathlib import Path

text = Path(__file__).with_name("result.txt").read_text(encoding="utf-8")
if text.strip() != "delivery-ready":
    raise SystemExit("Result does not match the agreed example content")
print("Example content verified")
