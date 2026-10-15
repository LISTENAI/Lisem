PYTHON ?= python3
CARGO_TARGET_DIR ?= $(CURDIR)/artifacts/desktop-build
export CARGO_TARGET_DIR

.DEFAULT_GOAL := build
.NOTPARALLEL:

QEMU_TESTS := \
    qemu-adc qemu-aes qemu-aon-timer qemu-audio qemu-bluetooth \
    qemu-bluetooth-activity qemu-bluetooth-link qemu-bluetooth-rx qemu-calendar qemu-camera qemu-camera-capture qemu-camera-input \
    qemu-clock-notify qemu-counter-chain qemu-cpu qemu-cpu-clocks \
    qemu-events qemu-desktop qemu-display qemu-dma qemu-dma2d qemu-dma2d-memory qemu-dualtimer qemu-dvp-clock \
    qemu-gpio qemu-host-audio qemu-host-display qemu-host-network \
    qemu-hsu qemu-i2c qemu-icount qemu-jit-state qemu-jpeg qemu-jpeg-encode \
    qemu-mailbox qemu-pacing qemu-psram qemu-remap qemu-rf \
    qemu-radio-capacity qemu-rom qemu-sd qemu-serial qemu-signals qemu-soc-clocks qemu-storage \
    qemu-sysctl qemu-touch qemu-trng qemu-uart qemu-usb qemu-usb-lab qemu-usb-adb \
    qemu-venusa qemu-watchdog qemu-watchpoint qemu-wifi qemu-wifi-ap qemu-wifi-disconnect qemu-wifi-environment \
    qemu-wifi-dma qemu-wifi-doze qemu-wifi-rx qemu-wifi-tx qemu-wifi-wpa2

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

qemu-venusa: qemu-serial
