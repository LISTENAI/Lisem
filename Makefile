PYTHON ?= python3
CARGO_TARGET_DIR ?= $(CURDIR)/artifacts/desktop-build
export CARGO_TARGET_DIR

.DEFAULT_GOAL := build
.NOTPARALLEL:

QEMU_TESTS := \
    qemu-adc qemu-aon-timer qemu-audio qemu-bluetooth \
    qemu-bluetooth-activity qemu-bluetooth-link qemu-bluetooth-rx qemu-calendar qemu-camera qemu-camera-capture \
    qemu-clock-notify qemu-counter-chain qemu-cpu qemu-cpu-clocks \
    qemu-desktop qemu-display qemu-dma qemu-dma2d qemu-dualtimer qemu-dvp-clock \
    qemu-gpio qemu-host-audio qemu-host-display qemu-host-network \
    qemu-hsu qemu-i2c qemu-icount qemu-jit-state qemu-jpeg \
    qemu-mailbox qemu-pacing qemu-psram qemu-remap qemu-rf \
    qemu-rom qemu-sd qemu-soc-clocks qemu-storage \
    qemu-sysctl qemu-trng qemu-uart qemu-usb \
    qemu-watchdog qemu-watchpoint qemu-wifi qemu-wifi-ap \
    qemu-wifi-dma qemu-wifi-rx qemu-wifi-tx

.PHONY: build headless run test test-core qemu-build check-qemu $(QEMU_TESTS)
build:
	$(PYTHON) tools/desktop.py --build-only

headless:
	cargo +1.95.0 build --locked -p lisem-cli

run:
	$(PYTHON) tools/desktop.py --no-build

qemu-build:
	$(PYTHON) tools/build_qemu.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	cargo +1.95.0 test --locked --workspace

test-core:
	cargo +1.95.0 test --locked -p lisem-core -p lisem-cli

$(QEMU_TESTS):
	$(PYTHON) tests/run_$(subst -,_,$@).py

check-qemu: $(QEMU_TESTS)
