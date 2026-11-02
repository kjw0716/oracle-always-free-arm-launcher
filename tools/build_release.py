"""Build a source-only release. Personal runtime/config/key files are never globbed."""
from pathlib import Path
import configparser
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "grab.py", "wizard.py", "config.example.ini", "requirements.txt",
    "start.cmd", "start.sh", "README.md", "LICENSE", "NOTICE.txt",
    ".gitignore", ".gitattributes", ".github/workflows/tests.yml",
    "tests/test_core.py", "tests/test_usability.py", "tools/build_release.py",
    "README.zh-CN.md", "docs/USAGE.en.md", "docs/USAGE.zh-CN.md",
    "docs/images/menu.png", "docs/images/timing.png",
)


def build():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(ROOT / "config.example.ini", encoding="utf-8")
    if any(cfg["oracle"].values()) or cfg["instance"]["ssh_public_key_file"]:
        raise ValueError("Example config contains local account or key information")
    files = []
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or ROOT not in path.resolve().parents:
            raise ValueError(f"Unexpected source path: {name}")
        data = path.read_bytes()
        # Test fixtures use deliberately short dummy OCIDs and incomplete PEM markers.
        if re.search(rb"ocid1\.[A-Za-z0-9_.-]{60,}", data):
            raise ValueError(f"Possible real OCID in {name}")
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{100,}", data):
            raise ValueError(f"Private key material in {name}")
        if re.search(rb"ssh-(?:rsa|ed25519) [A-Za-z0-9+/=]{50,}", data):
            raise ValueError(f"Personal SSH public key in {name}")
        if re.search(rb"[A-Za-z]:\\Users\\", data):
            raise ValueError(f"Personal Windows path in {name}")
        files.append((name, data))
    output = ROOT / "dist" / "oracle-always-free-arm-launcher.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr("oracle-always-free-arm-launcher/" + name, data)
    print(f"Built {output.name}: {len(files)} release files; no local config, keys or runtime.")
    return output


if __name__ == "__main__":
    build()
