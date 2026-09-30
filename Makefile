# agentic-reporting: every target is offline and $0 unless it says otherwise.
# Uses the repo venv if it exists, else whatever `python` is on PATH (CI, Docker).
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)
OLLAMA_HOST ?= http://localhost:11434
OLLAMA_MODEL ?= qwen2.5:7b-instruct
IMAGE ?= agentic-reporting:0.1.0
SPEC ?= specs/sales_mbr.yaml
DATA ?= samples/sales.sqlite
Q ?= Which region had the highest net revenue last month?

.PHONY: help install samples test lint eval demo demo-ask resume list demo-private demo-bedrock \
        docker-build docker-demo clean

help:
	@echo "install samples test lint eval demo demo-ask resume THREAD=.. DECISION=.. list"
	@echo "demo-private demo-bedrock docker-build docker-demo clean"

install:
	$(PY) -m pip install -e ".[all]"

samples:
	$(PY) samples/make_sales_db.py
	$(PY) samples/make_ops_db.py

test: samples
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

eval:
	$(PY) -m eval.report_eval --fake
	$(PY) -m eval.schema_linking_eval --fake

# Offline, deterministic: drafts the sales MBR with the fake provider and PAUSES at the gate.
demo: samples
	$(PY) -m agentic_reporting.run --spec $(SPEC) --db $(DATA) --provider fake

# Offline: question -> schema graph -> linked subset -> SQL (fake writer) -> rows, read-only.
# make demo-ask DATA=samples/ops.sqlite Q="Which team had the most incidents last month?"
demo-ask: samples
	$(PY) -m agentic_reporting.ask --db $(DATA) "$(Q)"

# make resume THREAD=sales_mbr-20260930T101500Z DECISION=approve   (or reject)
resume:
	@test -n "$(THREAD)" || (echo "usage: make resume THREAD=<thread> DECISION=approve|reject" && exit 2)
	$(PY) -m agentic_reporting.run --resume --thread $(THREAD) $(if $(DECISION),--decision $(DECISION),)

list:
	$(PY) -m agentic_reporting.run --list

# Local model, no cloud: needs `ollama serve` and the model pulled. Exits 0 with a hint if not.
demo-private: samples
	@if ! curl -s --max-time 3 $(OLLAMA_HOST)/api/tags >/dev/null 2>&1; then \
		echo "Ollama is not reachable at $(OLLAMA_HOST)."; \
		echo "Start it with \`ollama serve\` and pull the model: \`ollama pull $(OLLAMA_MODEL)\`."; \
		echo "(or: docker compose up -d ollama && docker compose exec ollama ollama pull $(OLLAMA_MODEL))"; \
		exit 0; \
	fi; \
	LLM_PROVIDER=ollama OLLAMA_HOST=$(OLLAMA_HOST) OLLAMA_MODEL=$(OLLAMA_MODEL) \
		$(PY) -m agentic_reporting.run --spec $(SPEC) --db $(DATA) --provider ollama

# Claude on AWS Bedrock: needs AWS credentials in the standard chain. Costs real (small) money.
demo-bedrock: samples
	LLM_PROVIDER=bedrock $(PY) -m agentic_reporting.run --spec $(SPEC) --db $(DATA) \
		--provider bedrock --max-usd 0.05

docker-build:
	docker build -t $(IMAGE) .

docker-demo: docker-build
	mkdir -p reports .agentic_reporting
	docker run --rm --user "$$(id -u):$$(id -g)" -v "$$PWD/reports:/app/reports" -v "$$PWD/.agentic_reporting:/app/.agentic_reporting" \
		$(IMAGE) --spec specs/sales_mbr.yaml --db samples/sales.sqlite --provider fake

clean:
	rm -rf reports .agentic_reporting samples/*.sqlite .pytest_cache .ruff_cache
