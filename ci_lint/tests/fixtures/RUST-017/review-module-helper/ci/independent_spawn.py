import subprocess

from ci.soldr import cargo_command


def main() -> int:
    suites = [
        (
            ["-p", "running-process-platform-internal", "--lib"],
            ["scheduler_error::tests", "--nocapture"],
        ),
    ]
    for package, test_filter in suites:
        command = cargo_command("test", *package, "--features", "independent-spawn")
        subprocess.run([*command, "--", *test_filter], check=False)
    return 0
