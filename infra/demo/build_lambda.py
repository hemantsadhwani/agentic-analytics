"""Build the demo Lambda zip: the package, its three pure dependencies, the data, the index.

    AWS_PROFILE=agentic python infra/demo/build_lambda.py     # -> infra/demo/build/lambda.zip

Runs on Windows or macOS: the dependencies are downloaded as Linux ARM64 wheels for
Python 3.12 (``pip --platform``), so no Docker is needed. boto3 is not packed: the Lambda
runtime has it.

The document index is built here, once, with Titan embeddings (about 3,400 tokens, under
$0.0001), so a cold start embeds nothing: ``DocIndex.sync`` finds every chunk unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUILD = HERE / "build"
PKG = BUILD / "pkg"
DEPS = ["networkx>=3.2,<4", "pydantic>=2.7,<3", "pyyaml>=6,<7"]
DB = ROOT / "samples" / "adventureworks.sqlite"


def main() -> int:
    if not DB.is_file():
        print(f"missing {DB}: run python samples/load_adventureworks.py")
        return 1
    shutil.rmtree(BUILD, ignore_errors=True)
    PKG.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(PKG),
                    "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                    "--python-version", "3.12", "--only-binary=:all:", *DEPS], check=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "agentic_reporting", PKG / "agentic_reporting", ignore=ignore)
    shutil.copytree(ROOT / "specs", PKG / "specs", ignore=ignore)
    (PKG / "samples").mkdir()
    shutil.copy2(DB, PKG / "samples" / DB.name)

    # the document index, built with the same embedder the Lambda uses
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("AWS_REGION", "ap-south-1")
    from agentic_reporting.doc_index import DocIndex
    from agentic_reporting.documents import load_chunks
    from agentic_reporting.embeddings import get_embedder
    chunks, quarantined = load_chunks(DB)
    idx_dir = PKG / ".agentic_reporting"
    idx_dir.mkdir()
    index = DocIndex(idx_dir / f"docs_{DB.stem}_titan.sqlite", get_embedder("titan"))
    stats = index.sync(chunks)
    index.close()
    print(f"index: {stats['added']} chunks, {stats['embed_tokens']} tokens, "
          f"${stats['embed_cost_usd']:.6f}; quarantined {len(quarantined)}")

    out = BUILD / "lambda.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(PKG.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(PKG).as_posix())
    mb = out.stat().st_size / 1e6
    print(f"wrote {out} ({mb:.1f} MB zipped; direct upload limit 50 MB)")
    return 0 if mb < 50 else 1


if __name__ == "__main__":
    sys.exit(main())
