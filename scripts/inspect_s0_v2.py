import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET = BASE_DIR / "data" / "locomo" / "locomo10.json"

with open(DATASET, "r", encoding="utf-8") as f:
    dataset = json.load(f)

s0 = dataset[0]
print(f"Sample ID: {s0['sample_id']}")

# Check all session dates
dates = []
for k, v in s0['conversation'].items():
    if k.endswith("_date_time"):
        dates.append(v)
        if "7 May" in v:
            print(f"Found it! {k}: {v}")

print(f"Total sessions: {len(dates)}")
print(f"Sample dates: {sorted(dates)[:5]} ... {sorted(dates)[-5:]}")

# Check the first question again
q0 = s0['qa'][0]
print(f"Q0: {q0['question']}")
ans = q0.get('answer') or q0.get('adversarial_answer')
print(f"A0: {ans}")

# Search for 'LGBTQ support group' in the conversation text
found_text = False
for k, v in s0['conversation'].items():
    if isinstance(v, list):
        for turn in v:
            if "support group" in turn.get("text", "").lower():
                print(f"Found 'support group' in {k}: {turn['text']}")
                found_text = True

if not found_text:
    print("'support group' NOT found in conversation turns.")
