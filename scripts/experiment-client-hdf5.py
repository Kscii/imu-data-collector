#!/usr/bin/env python3
"""Build a local-only client HDF5 prototype from an immutable delivery-v2 ZIP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from imu_data_collector.client_hdf5 import build_client_hdf5


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("destination_h5", type=Path)
    args = parser.parse_args()
    report = build_client_hdf5(args.source_zip, args.destination_h5)
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
