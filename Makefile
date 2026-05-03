.PHONY: test dist clean iterm2

PLUGIN_DIR := iterm2_webui
DIST_DIR   := dist
ZIP        := $(DIST_DIR)/iterm2-webui.zip
REPO       := $(abspath .)
AUTOLAUNCH := $(HOME)/Library/Application Support/iterm2/Scripts/AutoLaunch

test:
	uv run --project iterm2_webui  pytest iterm2_webui/tests
	uv run --project webui_protocol pytest webui_protocol/tests
	uv run --project webui_demo    pytest webui_demo/tests

dist: $(ZIP)

$(ZIP): $(shell find iterm2_webui webui_protocol/webui_protocol -name '*.py' -not -path '*/.venv/*' -not -path '*/__pycache__/*') iterm2_webui/setup.cfg
	mkdir -p $(DIST_DIR)
	cd iterm2_webui && zip -r ../$(ZIP) \
		setup.cfg \
		iterm2_webui/ \
		--exclude '*/__pycache__/*' --exclude '*.pyc'
	cd webui_protocol && zip -r ../$(ZIP) \
		webui_protocol/ \
		--exclude '*/__pycache__/*' --exclude '*.pyc'
	@echo "Built $(ZIP)"

iterm2:
	cd iterm2_webui && uv sync
	mkdir -p "$(AUTOLAUNCH)"
	@{ \
		echo '#!/usr/bin/env python3'; \
		echo 'import sys, os'; \
		echo '_V = "$(REPO)/iterm2_webui/.venv/bin/python"'; \
		echo 'if sys.executable != _V: os.execv(_V, [_V, __file__])'; \
		echo 'sys.path.insert(0, "$(REPO)/iterm2_webui")'; \
		echo 'sys.path.insert(0, "$(REPO)/webui_protocol")'; \
		echo 'import iterm2_webui.main  # noqa: F401'; \
	} > "$(AUTOLAUNCH)/webview_bridge.py"
	@echo "iTerm2 plugin installed → $(AUTOLAUNCH)/webview_bridge.py"
	@echo "Restart iTerm2 to activate"

clean:
	rm -rf $(DIST_DIR)
