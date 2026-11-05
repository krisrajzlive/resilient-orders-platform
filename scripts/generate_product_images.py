"""Render a deterministic product card image for every catalog item.

Two sets are produced:
  data/images/products/<id>.png   clean catalog cards (indexed)
  data/images/queries/<id>.jpg    perturbed copies (rotated, blurred, recoloured, cropped, JPEG)
                                  used as query-by-image inputs for evaluation.

Usage: python scripts/generate_product_images.py
"""
from __future__ import annotations

import hashlib
import json
import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SIZE = 384

CATEGORY_COLOURS = {
    "footwear": (34, 102, 170), "apparel": (150, 60, 90), "outdoor": (40, 120, 70),
    "kitchen": (190, 110, 30), "electronics": (70, 70, 140), "fitness": (180, 50, 50),
    "home": (110, 90, 60), "travel": (30, 130, 140), "pets": (140, 100, 160),
}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def render_card(product: dict) -> Image.Image:
    base = CATEGORY_COLOURS.get(product["category"], (80, 80, 80))
    img = Image.new("RGB", (SIZE, SIZE), base)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, SIZE - 24, SIZE - 24), radius=24, fill=(250, 250, 246))

    shape = int(hashlib.md5(product["id"].encode()).hexdigest(), 16) % 3
    box = (SIZE // 2 - 60, 50, SIZE // 2 + 60, 170)
    if shape == 0:
        d.ellipse(box, fill=base)
    elif shape == 1:
        d.rectangle(box, fill=base)
    else:
        d.polygon([(box[0], box[3]), (box[2], box[3]), (SIZE // 2, box[1])], fill=base)

    y = 195
    for line in textwrap.wrap(product["name"], 20):
        d.text((SIZE // 2, y), line, fill=(20, 20, 20), font=_font(28), anchor="mt")
        y += 36
    d.text((SIZE // 2, SIZE - 70), product["category"].upper(), fill=base, font=_font(22), anchor="mt")
    return img


def perturb(img: Image.Image, seed: str) -> Image.Image:
    rng = random.Random(seed)
    out = img.rotate(rng.uniform(-6, 6), resample=Image.BICUBIC, fillcolor=(235, 235, 235))
    margin = int(SIZE * 0.05)
    out = out.crop((margin, margin, SIZE - margin, SIZE - margin)).resize((SIZE, SIZE))
    out = ImageEnhance.Brightness(out).enhance(rng.uniform(0.8, 1.1))
    out = ImageEnhance.Color(out).enhance(rng.uniform(0.7, 1.2))
    return out.filter(ImageFilter.GaussianBlur(rng.uniform(0.6, 1.4)))


def main() -> None:
    catalog = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))
    products_dir = ROOT / "data" / "images" / "products"
    queries_dir = ROOT / "data" / "images" / "queries"
    products_dir.mkdir(parents=True, exist_ok=True)
    queries_dir.mkdir(parents=True, exist_ok=True)
    for p in catalog:
        card = render_card(p)
        card.save(products_dir / f"{p['id']}.png")
        perturb(card, p["id"]).save(queries_dir / f"{p['id']}.jpg", quality=70)
    print(f"wrote {len(catalog)} catalog images and {len(catalog)} query images")


if __name__ == "__main__":
    main()
