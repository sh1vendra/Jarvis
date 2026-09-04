# Test suite entry points. Run from backend/ (`make test`, `make test-all`,
# ...) or from the repo root (`make -C backend test`). See tests/README.md
# for what's actually in each suite and why the split is drawn where it is.

.PHONY: test test-integration test-all install-dev

# Fast suite: no real Mac state, no network, no LLM call. Safe to run
# constantly while developing, and what CI would run.
test:
	pytest tests/unit -v

# The real suite: actually touches Reminders/Spotify/the real microphone
# and speakers, and makes one real Gemini call. Needs the real local
# environment - see tests/README.md's preconditions before running this.
test-integration:
	pytest tests/integration -m integration -v

# Both, in one run - what to run before demoing this live.
test-all:
	pytest tests/unit tests/integration -m "" -v

# One-time setup: the runtime deps plus pytest/pytest-asyncio.
install-dev:
	pip install -r requirements.txt -r requirements-dev.txt
