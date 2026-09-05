# codex-orchestrator — pull-core workflow
#
# core/ is a `git subtree` vendor of fable5-opus5-orchestrator (the Claude
# repo, which owns scripts/instructions/skills/playbook/tests). A fix lands
# there, gets tagged `core-vN`. skills/playbook is a REAL COPY of
# core/skills/playbook (Codex's plugin cache copy drops symlinks), refreshed
# by this target.
# (see that repo's `make tag-core VERSION=vN`
# on the `core-tagging` branch), and is pulled in here.
#
# SOURCE defaults to the sibling checkout used during development; override
# with a git remote URL once this repo has one configured (`git remote add
# core-upstream git@github.com:Shredi/fable5-opus5-orchestrator.git`).
SOURCE ?= ../fable5-opus5-orchestrator
TAG ?=

.PHONY: pull-core
pull-core:
	@if [ -z "$(TAG)" ]; then \
		echo "usage: make pull-core TAG=core-vN"; \
		exit 1; \
	fi
	git subtree pull --prefix=core $(SOURCE) $(TAG) --squash -m "core: pull $(TAG)"
	echo "$(TAG)" > core/VERSION
	rm -rf skills/playbook && cp -R core/skills/playbook skills/playbook
	git add core/VERSION skills/playbook
	git commit -m "chore: core/VERSION -> $(TAG); skills/playbook refreshed from core"
	@echo "pulled $(TAG) into core/, core/VERSION updated"

# Two SEPARATE pytest invocations, not one combined `pytest core/tests tests`
# — core/tests/conftest.py and tests/conftest.py are both named "conftest"
# with no package __init__.py (core/ is vendored, untouched on purpose; see
# the pull-core note above), and pytest's default import mode can only hold
# one module named "conftest" in sys.modules at a time. Running them as two
# processes sidesteps the collision entirely; it also mirrors the two-repo
# model (core's tests prove core/ is unbroken standalone, this repo's tests
# prove the profiles/agents/codex-sync layer on top of it).
.PHONY: test
test:
	cd core && python3 -m pytest tests/ -q
	python3 -m pytest tests/ -q
