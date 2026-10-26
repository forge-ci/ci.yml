"""Fixture: the pre-prune job delegates to this script (one level deep)."""
import subprocess
import sys

subprocess.run([sys.executable, "-m", "ci_lint", "cache", "preprune", "--repo", "."], check=True)
