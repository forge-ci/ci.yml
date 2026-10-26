def cargo_command(*args: str) -> list[str]:
    return ["soldr", "cargo", *args]
