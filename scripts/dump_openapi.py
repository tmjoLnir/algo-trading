"""Write the API's OpenAPI schema to a file.

`make gen-types` runs this and then points `openapi-typescript` at the result,
so regenerating the dashboard's types needs neither a running server nor a
database — which is the difference between a generation step people run and one
they work around by hand-editing the types instead.

Generating the schema also forces FastAPI to resolve every route handler's
annotations, so a handler whose `datetime` sits behind `if TYPE_CHECKING` fails
here rather than on the first request. `tests/unit/test_api_contract.py` asserts
the same thing for the same reason; this just gets it for free.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from atp_api.main import create_app

#: Where `apps/web`'s `gen:types` script expects to find it. Relative to the
#: repository root, which is where `make` runs.
DEFAULT_OUTPUT = Path("apps/web/openapi.json")


def main(argv: list[str]) -> int:
    destination = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Sorted keys and a trailing newline: the file is an input to a generator,
    # and a diff that reorders itself run to run would make the generated types
    # churn for no reason.
    destination.write_text(
        json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
