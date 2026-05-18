# Makefile — developer convenience targets
# Run `make help` for a summary.

.PHONY: help install test test-cov lint run clean

PYTHON := python3
DATA_DIR := data
MAX_RESULTS := 50

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install all dependencies into the current Python environment
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

test:  ## Run the full test suite
	$(PYTHON) -m pytest tests/ -v

test-cov:  ## Run tests with coverage report
	$(PYTHON) -m pytest tests/ -v --cov=crawler --cov-report=term-missing --cov-report=html

lint:  ## Run ruff linter (install separately: pip install ruff)
	ruff check crawler/ tests/ scripts/

run:  ## Run the crawler (requires GITHUB_TOKEN env var)
	$(PYTHON) scripts/run_crawler.py \
	  --max-results $(MAX_RESULTS) \
	  --data-dir $(DATA_DIR)

clean:  ## Remove __pycache__, .pyc files, coverage reports
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf .coverage htmlcov/ .pytest_cache/