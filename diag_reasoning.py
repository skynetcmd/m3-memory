"""Confirm direct OpenAI judge call works without tool_choice."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bin"))
from auth_utils import get_api_key
from openai import OpenAI

c = OpenAI(api_key=get_api_key("OPENAI_API_KEY"))
prompt = (
    "Question: When did Melanie paint a sunrise?\n"
    "Correct Ground-truth Answer: 2022\n"
    "Model Response: Melanie painted the lake sunrise sometime in 2022.\n"
    "Is the model response correct? Answer 'yes' or 'no' only."
)
r = c.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=100)
print("content:", repr(r.choices[0].message.content))
print("finish:", r.choices[0].finish_reason)
