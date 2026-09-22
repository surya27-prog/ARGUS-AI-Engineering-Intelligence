"""Services — the seam between the API and the parser.

The parser is a sibling package, not a published distribution. Week 6 turns it
into a real editable install; until then the path bootstrap lives here, in the
package every module that imports `parser` belongs to, so importing any one of
them in any order works.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PARSER_DIR = Path(__file__).resolve().parents[3] / "parser"
if str(_PARSER_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSER_DIR))
