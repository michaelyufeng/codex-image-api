#!/usr/bin/env python3
"""Example: drive codex-image-api with the official OpenAI Python SDK.

    pip install openai
    python3 examples/python_openai.py
"""
import base64

from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:10532/v1", api_key="unused")

# --- text -> image --------------------------------------------------------- #
r = client.images.generate(
    model="gpt-image-2",
    prompt="一只戴橙色围巾的卡通水獭，简约扁平插画风格，纯色背景",
    size="1024x1024",
    quality="low",
)
b64 = r.data[0].b64_json if r.data else None
assert b64, "no image returned"
with open("out.png", "wb") as f:
    f.write(base64.b64decode(b64))
print("saved out.png")

# --- image -> image (edit) ------------------------------------------------- #
r2 = client.images.edit(
    model="gpt-image-2",
    image=open("out.png", "rb"),
    prompt="把这只水獭的围巾改成蓝色，其余保持一致",
    size="1024x1024",
    quality="low",
)
b64e = r2.data[0].b64_json if r2.data else None
assert b64e, "no image returned"
with open("out_edited.png", "wb") as f:
    f.write(base64.b64decode(b64e))
print("saved out_edited.png")
