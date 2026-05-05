#!/usr/bin/env python3
"""
TindaAgent Release Signing Tool

Signs a manifest.json with the TindaAgent Ed25519 release private key.
Outputs manifest.sig (raw signature bytes) for inclusion in GitHub Release assets.

Usage:
  python scripts/sign_release.py <path/to/manifest.json> [output.sig]

Private key location: ~/.tinda/agent/trust/release_private.key
"""

import base64
import json
import os
import sys
import stat
from pathlib import Path


def _resolve_trust_dir() -> Path:
    tinda_home = os.environ.get("TINDA_HOME", "")
    if tinda_home:
        return Path(tinda_home) / "trust"
    return Path.home() / ".tinda" / "agent" / "trust"


def _load_private_key() -> bytes:
    key_path = _resolve_trust_dir() / "release_private.key"
    if not key_path.exists():
        print(f"ERROR: Private key not found at {key_path}", file=sys.stderr)
        print("Generate one first: python -c \"from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; ...\"",
              file=sys.stderr)
        sys.exit(1)

    mode = key_path.stat().st_mode
    if mode & 0o077:
        print(f"WARNING: Private key has loose permissions ({oct(mode)[-3:]}). chmod 600 recommended.",
              file=sys.stderr)

    b64 = key_path.read_text(encoding="utf-8").strip()
    try:
        return base64.b64decode(b64)
    except Exception as e:
        print(f"ERROR: Failed to decode private key: {e}", file=sys.stderr)
        sys.exit(1)


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/sign_release.py <path/to/manifest.json> [output.sig]", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path(sys.argv[1])
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_bytes = _canonical_json_bytes(manifest)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_bytes = _load_private_key()
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    sig = private_key.sign(manifest_bytes)

    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else manifest_path.parent / "manifest.sig"
    out_path.write_bytes(sig)

    from hashlib import sha256
    sig_id = "sig_" + sha256(manifest_bytes + sig).hexdigest()[:16]
    manifest_sha = sha256(manifest_bytes).hexdigest()

    print(f"Signed: {manifest_path}")
    print(f"Signature: {out_path}")
    print(f"Signature ID: {sig_id}")
    print(f"Manifest SHA-256: {manifest_sha}")
    print(f"Private key: ~/.tinda/agent/trust/release_private.key")

    # Verify signature locally
    public_key = private_key.public_key()
    public_key.verify(sig, manifest_bytes)
    print("Self-verify: OK")


if __name__ == "__main__":
    main()
