.PHONY: test validate eval install uninstall lint

test:            ## unit + integration tests (stdlib only)
	python3 -m unittest discover -s tests -v

validate:        ## Claude's own manifest/agent/hook validation
	claude plugin validate plugins/orchestrator
	claude plugin validate .

lint:            ## shell + python lint if the tools are installed
	@command -v shellcheck >/dev/null && shellcheck install.sh uninstall.sh plugins/orchestrator/hooks/*.sh plugins/orchestrator/evals/*/scaffold.sh || echo "shellcheck not installed"
	@command -v ruff >/dev/null && ruff check bin/ccx tests || echo "ruff not installed"

eval:            ## routing evals: one run per case, capped spend
	claude plugin eval plugins/orchestrator --scaffold --runs 1 --ablation none --allow-tools Edit Write --max-cost-usd 10

install:
	./install.sh --with-ghostty

uninstall:
	./uninstall.sh
