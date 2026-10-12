# Chart tasks. Run from charts/.   Needs: helm 3.8+, python3 (for the contract tests).
CHART ?= dial-platform
OUT   ?= dist
VERSION ?= $(shell sed -n 's/^version: *//p' dial-platform/Chart.yaml | head -1)

.PHONY: lint test package push

lint:            ## helm lint both charts (the platform chart is linted with its placeholders filled in)
	python3 ../installer/tools/lint_chart.py
	helm lint dial-cluster-base

test:            ## chart <-> installer contract tests (render with real helm, no cluster needed)
	@test -f ../installer/Makefile || (echo "ERROR: ../installer (the installer and its contract tests) is missing. Keep the installer/ directory next to charts/; without it the chart is NOT validated."; exit 1)
	$(MAKE) -C ../installer test

package:         ## build dist/naya-helm-deployment-<version>.tgz (contains release.yaml and placeholders.yaml)
	helm package dial-platform -d $(OUT)
	helm package dial-cluster-base -d $(OUT)

push:            ## publish the platform chart:  make push REGISTRY=oci://709825985650.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam
	@test -n "$(REGISTRY)" || (echo "Set REGISTRY=oci://..."; exit 1)
	@test -f $(OUT)/naya-helm-deployment-$(VERSION).tgz || (echo "Run 'make package' first"; exit 1)
	helm push $(OUT)/naya-helm-deployment-$(VERSION).tgz $(REGISTRY)
	# dial-cluster-base is NOT pushed: it ships inside the installer package
