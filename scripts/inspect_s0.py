import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET = BASE_DIR / "data" / "locomo" / "locomo10.json"

with open(DATASET, "r", encoding="utf-8") as f:
    dataset = json.load(f)

# The first sample in the JSON (index 0)
s0 = dataset[0]
print(f"Sample ID (index 0): {s0['sample_id']}")
print("Questions:")
for i, qa in enumerate(s0['qa']):
    print(f"  {i}: {qa['question']} -> {qa['answer']}")

# Check if 7 May 2023 exists in s0 sessions
found_7_may = False
for k, v in s0['conversation'].items():
    if k.endswith("_date_time") and "7 May 2023" in v:
        found_7_may = True
        print(f"Found 7 May 2023 in {k}")

if not found_7_may:
    print("7 May 2023 NOT found in any session date_time field for s0.")
