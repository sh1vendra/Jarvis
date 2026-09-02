"""Set (or list) a Tier 1 preference by hand.

There is no "remember this" voice command yet - that's future scope. This
is the manual way to put a fact in the store so the Planner can use it:

    python -m memory.set_preference "default_flight_destination" "Austin, Texas"
    python -m memory.set_preference "who is mom" "Susan Bhagat, +1 512 555 0134"
    python -m memory.set_preference            # no args: just list what's stored

Run it from the backend/ directory.
"""

import sys

from memory.store import all_preferences, db_path, set_preference


def main(argv: list[str]) -> None:
    if len(argv) == 2:
        key, value = argv
        set_preference(key, value)
        print(f"set  {key!r} = {value!r}")
    elif argv:
        raise SystemExit('usage: python -m memory.set_preference "<key>" "<value>"   (or no args to list)')

    prefs = all_preferences()
    print(f"\npreferences in {db_path()}:")
    if not prefs:
        print("  (none)")
    for k, v in prefs.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
