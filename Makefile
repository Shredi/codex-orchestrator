# fable5-opus5-orchestrator — core-tagging helper
#
# codex-orchestrator vendors scripts/, instructions/, skills/playbook/ and
# tests/ (conftest.py + tests) from this repo via `git subtree`, pinned to a
# `core-vN` tag. Cutting a new tag here is the release step for that pull;
# it does not touch main and is never pushed automatically.
VERSION ?=

.PHONY: tag-core
tag-core:
	@if [ -z "$(VERSION)" ]; then \
		echo "usage: make tag-core VERSION=core-vN"; \
		exit 1; \
	fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
		echo "tag-core must be run from main (git checkout main first)"; \
		exit 1; \
	fi
	git tag $(VERSION)
	@echo "tagged $(VERSION) on main (local only — push explicitly if intended: git push origin $(VERSION))"
