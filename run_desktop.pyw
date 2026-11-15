import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "logs" / "desktop_error.log"

try:
    from autocode.desktop.app import main
    raise SystemExit(main())
except SystemExit:
    raise
except Exception:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        traceback.print_exc(file=fh)
    raise
