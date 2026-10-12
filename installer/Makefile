# Developer tasks for the DIAL installer package (customers never need make).
PYTHON ?= python3

.PHONY: test lint dist

test:            ## run the unit and contract tests (no AWS access needed)
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

lint:            ## validate every CloudFormation template and both Helm charts (needs: pip install cfn-lint, helm)
	@python3 tools/lint_chart.py && helm lint ../charts/dial-cluster-base
	@for f in modules/*/*/stack.yaml; do echo "== $$f"; cfn-lint "$$f" --regions us-east-1 || exit 1; done

dist: test       ## build dist/dial-install-<version>.tar.gz for customers
	$(PYTHON) build.py
