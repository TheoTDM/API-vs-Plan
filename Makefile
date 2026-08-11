PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin

.PHONY: install uninstall test run help

help:
	@echo "make install    symlink bin/avp into $(BINDIR) so 'avp' works anywhere"
	@echo "make uninstall  remove that symlink"
	@echo "make test       run the test suite"
	@echo "make run        run the report without installing"

install:
	@mkdir -p $(BINDIR)
	@ln -sf $(CURDIR)/bin/avp $(BINDIR)/avp
	@echo "linked $(BINDIR)/avp -> $(CURDIR)/bin/avp"
	@command -v avp >/dev/null 2>&1 || \
		echo "note: $(BINDIR) is not on your PATH -- add it to use 'avp' directly"

uninstall:
	@rm -f $(BINDIR)/avp
	@echo "removed $(BINDIR)/avp"

test:
	@PYTHONPATH=src python3 -m unittest discover -s tests -v

run:
	@./bin/avp
