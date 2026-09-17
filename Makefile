# Developer and deployment entry points. Everything runs from the project
# virtualenv created by `make install` (scripts/install.sh).

VENV ?= .venv
PY   := $(VENV)/bin/python

SYSTEMD_DIR := /etc/systemd/system
SUDOERS_DIR := /etc/sudoers.d
UNITS       := deploy/systemd/rpi-cam-lattice-service.service deploy/systemd/mediamtx-srt.service
SUDOERS     := deploy/sudoers.d/rpi-cam-lattice-service

.PHONY: install test lint typecheck check install-units

install:
	./scripts/install.sh

test:
	$(PY) -m pytest -q

lint:
	$(VENV)/bin/ruff check . && $(VENV)/bin/ruff format --check .

typecheck:
	$(VENV)/bin/mypy

check: lint typecheck test

# Copies the two systemd units and the sudoers rule into place (needs sudo)
# and reloads systemd. Edit the placeholders (project path, User=/Group=,
# sudoers account) in the files under deploy/ first. Does not enable or start
# anything: `sudo systemctl enable --now rpi-cam-lattice-service` afterwards.
# mediamtx-srt must NOT be enabled; the daemon starts it on a Start task.
install-units:
	@echo "Validating $(SUDOERS)"
	visudo -cf $(SUDOERS)
	@echo "Installing units to $(SYSTEMD_DIR): $(UNITS)"
	sudo install -m 0644 $(UNITS) $(SYSTEMD_DIR)/
	@echo "Installing sudoers rule to $(SUDOERS_DIR)/rpi-cam-lattice-service (mode 0440)"
	sudo install -m 0440 -o root -g root $(SUDOERS) $(SUDOERS_DIR)/rpi-cam-lattice-service
	@echo "Reloading systemd"
	sudo systemctl daemon-reload
	@echo "Done. If mediamtx-srt was enabled by an older install: sudo systemctl disable --now mediamtx-srt"
	@echo "Then: sudo systemctl enable --now rpi-cam-lattice-service"
