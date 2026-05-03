.PHONY: test dist clean

PLUGIN_DIR := iterm2_webgui
DIST_DIR   := dist
ZIP        := $(DIST_DIR)/iterm2-webgui.zip

test:
	uv run --project iterm2_webgui  pytest iterm2_webgui/tests
	uv run --project webgui_protocol pytest webgui_protocol/tests
	uv run --project webgui_demo    pytest webgui_demo/tests

dist: $(ZIP)

$(ZIP): $(shell find iterm2_webgui webgui_protocol/webgui_protocol -name '*.py' -not -path '*/.venv/*' -not -path '*/__pycache__/*') iterm2_webgui/setup.cfg
	mkdir -p $(DIST_DIR)
	cd iterm2_webgui && zip -r ../$(ZIP) \
		setup.cfg \
		iterm2_webgui/ \
		--exclude '*/__pycache__/*' --exclude '*.pyc'
	cd webgui_protocol && zip -r ../$(ZIP) \
		webgui_protocol/ \
		--exclude '*/__pycache__/*' --exclude '*.pyc'
	@echo "Built $(ZIP)"

clean:
	rm -rf $(DIST_DIR)
