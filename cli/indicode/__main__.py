import os, sys
from .tui import IndicodeTUI

def main():
    url = os.environ.get("INDICODE_URL") or "http://localhost:8080/api/agent"
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        url = sys.argv[1]
    IndicodeTUI(url).run()

if __name__ == "__main__":
    main()