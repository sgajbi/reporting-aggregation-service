.PHONY: install lint typecheck test test-unit test-integration test-e2e test-coverage security-audit check ci ci-local docker-build clean

install:
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev]"
	python -m pip install pre-commit
	pre-commit install

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy --config-file mypy.ini

test:
	$(MAKE) test-unit

test-unit:
	python -m pytest tests/unit

test-integration:
	python -m pytest tests/integration

test-e2e:
	python -m pytest tests/e2e

test-coverage:
	COVERAGE_FILE=.coverage.unit python -m pytest tests/unit --cov=src/app --cov-report=
	COVERAGE_FILE=.coverage.integration python -m pytest tests/integration --cov=src/app --cov-report=
	COVERAGE_FILE=.coverage.e2e python -m pytest tests/e2e --cov=src/app --cov-report=
	python -m coverage combine .coverage.unit .coverage.integration .coverage.e2e
	python -m coverage report --fail-under=99

security-audit:
	python -m pip_audit

check: lint typecheck test

ci: lint typecheck test-integration test-e2e test-coverage security-audit

ci-local: ci

docker-build:
	docker build -t reporting-aggregation-service:ci-test .

clean:
	python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache', '.ruff_cache', '.mypy_cache']]; [pathlib.Path(p).unlink(missing_ok=True) for p in ['.coverage', '.coverage.unit', '.coverage.integration', '.coverage.e2e']]"
