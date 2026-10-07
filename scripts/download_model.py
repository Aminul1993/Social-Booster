"""Pre-download the ResNet-50 weights into ``$TORCH_HOME``.

Used at Docker build time so containers start without network access to
download.pytorch.org, and handy locally to warm the cache:

    python scripts/download_model.py --weights IMAGENET1K_V2
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--weights",
        default="IMAGENET1K_V2",
        help="ResNet50_Weights member to fetch (IMAGENET1K_V2, IMAGENET1K_V1, DEFAULT)",
    )
    args = parser.parse_args(argv)

    import torch
    from torchvision.models import ResNet50_Weights

    weights = ResNet50_Weights[args.weights]
    print(f"Downloading {weights} -> {torch.hub.get_dir()}")
    weights.get_state_dict(progress=False, check_hash=True)
    print(f"OK: {len(weights.meta['categories'])} categories available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
