"""Retired CSV theme template generator.

Static theme CSV generation is no longer part of the runtime or test surface.
Use ``trading.theme_engine`` source adapters and the dynamic source sync flow.
"""

MESSAGE = "theme_mappings.csv is retired. Use DynamicThemeEngine source sync instead."


def main() -> int:
    print(MESSAGE)
    return 1
