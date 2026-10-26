import subprocess

subprocess.run(["soldr", "cargo", "clippy", "--workspace"], check=True)
subprocess.run(["soldr", "cargo", "test", "--workspace"], check=True)
