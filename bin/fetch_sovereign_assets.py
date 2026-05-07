#!/usr/bin/env python3
"""
fetch_sovereign_assets.py — Hydrate the _assets/embedder directory for sovereign setup.
Usage: python bin/fetch_sovereign_assets.py
"""

import os
import sys
import json
import hashlib
import pathlib
import requests
from tqdm import tqdm

BASE = pathlib.Path(__file__).parent.parent.resolve()
ASSETS_DIR = BASE / "_assets" / "embedder"
MANIFEST_FILE = ASSETS_DIR / "manifest.json"

# URLs for LM Studio CLI (lms) and BGE-M3 models
# Note: These URLs are representative and should be updated to the latest stable versions.
LMS_RELEASES_URL = "https://github.com/lmstudio-ai/lms-cli/releases/download/v0.3.0/"

ASSETS = {
    "bin": {
        "lms-windows-x64.exe": f"{LMS_RELEASES_URL}lms-windows-x64.exe",
        "lms-macos-arm64": f"{LMS_RELEASES_URL}lms-macos-arm64",
        "lms-linux-x64": f"{LMS_RELEASES_URL}lms-linux-x64",
        "lms-linux-arm64": f"{LMS_RELEASES_URL}lms-linux-arm64",
    },
    "models": {
        "bge-m3-q4_k_m.gguf": "https://huggingface.co/bartowski/bge-m3-GGUF/resolve/main/bge-m3-Q4_K_M.gguf",
        # MLX model is a folder, would need individual file fetching or a tarball.
        # For this script, we'll focus on the GGUF as the universal baseline.
    }
}

def get_sha256(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def download_file(url, dest_path):
    os.makedirs(dest_path.parent, exist_ok=True)
    response = requests.get(url, stream=True)
    total_size = int(response.headers.get('content-length', 0))
    
    with open(dest_path, "wb") as f, tqdm(
        desc=dest_path.name,
        total=total_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
    ) as bar:
        for data in response.iter_content(chunk_size=1024):
            size = f.write(data)
            bar.update(size)

def main():
    print("--- M3 Sovereign Payload Hydrator ---")
    print(f"Target Directory: {ASSETS_DIR}")
    
    manifest = {}

    # 1. Download Binaries
    for name, url in ASSETS["bin"].items():
        dest = ASSETS_DIR / "bin" / name
        if not dest.exists():
            print(f"Fetching binary: {name}")
            try:
                download_file(url, dest)
            except Exception as e:
                print(f"Error fetching {name}: {e}")
                continue
        
        manifest[name] = get_sha256(dest)
        print(f"Hashed {name}: {manifest[name][:12]}...")

    # 2. Download Models
    for name, url in ASSETS["models"].items():
        dest = ASSETS_DIR / "models" / name
        if not dest.exists():
            print(f"Fetching model: {name}")
            try:
                download_file(url, dest)
            except Exception as e:
                print(f"Error fetching {name}: {e}")
                continue
        
        manifest[name] = get_sha256(dest)
        print(f"Hashed {name}: {manifest[name][:12]}...")

    # 3. Generate Manifest
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest generated: {MANIFEST_FILE}")
    print("Hydration complete. The _assets/embedder folder is ready for sovereign installation.")

if __name__ == "__main__":
    main()
