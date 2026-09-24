"""Usage: python scripts/evaluate_traffic_accuracy.py completed_trip_pairs.json"""

import argparse
import json
from pathlib import Path

from planning.domain.traffic_accuracy import evaluate_accuracy

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate_accuracy(
                json.loads(args.observations.read_text(encoding="utf-8"))
            ),
            indent=2,
        )
    )
