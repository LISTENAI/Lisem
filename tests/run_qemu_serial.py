#!/usr/bin/env python3
"""Verify shared SPI and I2S controllers through both chip integrations."""
from qemu_qtest import run_tests

TESTS = ('arcs-spi-test', 'arcs-i2s-test', 'venusa-spi-test',
         'venusa-spi-slave-test', 'venusa-i2s-test')


def main():
    run_tests(TESTS)
    print('ARCS/VenusA SPI and I2S controllers, digital peers and chip routing: PASS')


if __name__ == '__main__':
    main()
