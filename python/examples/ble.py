"""Generic GATT exploration; the caller boots firmware and selects its mode."""


def inspect_peripheral(run):
    """Discover a real peripheral and return its current attribute metadata."""
    with run.ble as central:
        scan = central.scan()
        advert = scan.expect(
            lambda e: e['kind'] == 'advertisement' and e['pdu_type'] == 0,
            timeout=10,
        )
        with central.connect(
            advert['address'], address_type=advert['address_type']
        ) as connection:
            services = connection.discover()
            characteristics = connection.discover('characteristics')
            values = {}
            for characteristic in characteristics:
                if characteristic['properties'] & 0x02:
                    handle = characteristic['value_handle']
                    values[handle] = connection.read(handle)
            return {
                'services': services,
                'characteristics': characteristics,
                'values': values,
            }
