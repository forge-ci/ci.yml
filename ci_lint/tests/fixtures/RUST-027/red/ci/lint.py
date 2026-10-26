import subprocess
subprocess.run(["soldr", "cargo", "doc", "--no-deps"], check=True)
