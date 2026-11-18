# Developer shortcuts. Nothing here is needed to use Heliograph.
PY ?= python3

.PHONY: test run lint check-web synth clean

test:            ## run the whole suite (about 12 seconds)
	$(PY) -X dev -m unittest discover -s tests -t tests

run:             ## start from this checkout without installing
	./bin/heliograph

lint:            ## needs ruff (pip install ruff); optional
	ruff check src tests tools

check-web:       ## syntax-check the interface scripts (needs node)
	for f in src/heliograph/web/*.js; do node --check $$f || exit 1; done

synth:           ## build a throw-away 100k-file library for scale tests (isolated home, see docs/performance.md)
	HELIOGRAPH_HOME=$$(mktemp -d) $(PY) tools/synth_library.py --files 100000

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf build dist src/*.egg-info
