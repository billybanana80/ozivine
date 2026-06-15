"""
Ozivine shared ANSI colour codes.

Import and use as:
    from helpers.colors import bcolors
    print(f"{bcolors.OKGREEN}All good{bcolors.ENDC}")
"""


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    LIGHTBLUE = "\033[94m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    ORANGE = "\033[38;5;208m"
