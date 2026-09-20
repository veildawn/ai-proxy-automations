.PHONY: validate check

validate:
	python3 scripts/validate.py

check: validate
	go run scripts/verify_signatures.go
