"""Read the product version shared by Cargo packages and native packaging."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def product_version():
    text = (ROOT / 'Cargo.toml').read_text(encoding='utf-8')
    section = text.split('[workspace.package]', 1)[1].split('\n[', 1)[0]
    match = re.search(r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', section, re.M)
    if not match:
        raise ValueError('Missing workspace product version')
    return match[1]
