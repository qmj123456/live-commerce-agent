"""Run the launcher without installing the agent or third-party dependencies."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from live_agent.chrome_launcher import main  # noqa: E402

if __name__ == "__main__":
    main()
