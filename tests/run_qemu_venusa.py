#!/usr/bin/env python3
"""Run bounded QEMU regressions for VenusA and its shared controllers."""
from qemu_qtest import run_tests
TESTS = ('listenai-eclic-test', 'venusa-peripherals-test', 'venusa-gpadc-test',
         'venusa-audio-test', 'listenai-dma-peripheral-test',
         'venusa-calendar-test', 'venusa-pwm-test', 'venusa-luna-test',
         'venusa-i2c-test', 'listenai-uart-dma-test', 'venusa-dvp-test',
         'venusa-camera-test', 'venusa-image-test', 'venusa-touch-test')


def main():
    run_tests(TESTS)
    print('VenusA CPU/IRQ, storage, timers, ADC, audio, I2C, UART/DMA, DVP, RGB/DMA2D and optional board devices: PASS')


if __name__ == '__main__':
    main()
