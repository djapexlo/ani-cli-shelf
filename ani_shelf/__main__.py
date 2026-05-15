import subprocess
import sys
from pathlib import Path


def main():
    app = Path(__file__).parent / "app.py"
    sys.exit(subprocess.call([
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.headless", "true",
        *sys.argv[1:],
    ]))


if __name__ == "__main__":
    main()
