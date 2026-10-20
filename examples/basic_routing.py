"""OpenAI SDK pointed at CaiPS: only the base URL changes."""
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="YOUR_CAIPS_TOKEN")

for alias in ("fast", "cheap", "best"):  # virtual models -> routing policies
    r = client.chat.completions.create(
        model=alias, messages=[{"role": "user", "content": "Say hi in 3 words."}]
    )
    print(alias, "->", r.choices[0].message.content)
