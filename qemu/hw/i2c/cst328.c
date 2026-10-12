/* SPDX-License-Identifier: GPL-2.0-or-later */
/* CST328 single-contact reports and report-ready interrupt edges. */
#include "qemu/osdep.h"
#include "hw/i2c/cst328.h"
#include "hw/irq.h"
#include "hw/resettable.h"
#include "qemu/module.h"

struct CST328State {
    I2CSlave parent_obj;
    uint16_t address;
    unsigned address_bytes;
    uint16_t x, y;
    bool pressed;
    uint8_t report[4];
    qemu_irq int_irq;
};

static void reset(DeviceState *dev)
{
    CST328State *s = CST328(dev);
    s->address = 0;
    s->address_bytes = 0;
    s->x = s->y = 0;
    s->pressed = false;
    if (s->int_irq) { qemu_set_irq(s->int_irq, 1); }
}

static int event(I2CSlave *dev, enum i2c_event event)
{
    CST328State *s = CST328(dev);
    if (event == I2C_START_SEND) {
        s->address_bytes = 0;
    } else if (event == I2C_START_RECV) {
        if (s->address_bytes != 2 || s->address < 0xd000 || s->address > 0xd003) {
            return 1;
        }
        s->report[0] = s->pressed ? 0x06 : 0x00;
        s->report[1] = s->x >> 4;
        s->report[2] = s->y >> 4;
        s->report[3] = (s->x << 4) | (s->y & 0x0f);
    }
    return 0;
}

static int touch_send(I2CSlave *dev, uint8_t value)
{
    CST328State *s = CST328(dev);
    if (s->address_bytes == 0) {
        s->address = value;
    } else if (s->address_bytes == 1) {
        s->address |= (uint16_t)value << 8;
    }
    if (s->address_bytes >= 2) { return 1; }
    s->address_bytes++;
    if (s->address_bytes == 2 && (s->address < 0xd000 || s->address > 0xd003)) {
        return 1;
    }
    return 0;
}

static uint8_t touch_recv(I2CSlave *dev)
{
    CST328State *s = CST328(dev);
    uint8_t value = s->address >= 0xd000 && s->address <= 0xd003 ?
                    s->report[s->address - 0xd000] : 0;
    s->address++;
    return value;
}

bool cst328_get_contact(DeviceState *dev, uint16_t *x, uint16_t *y)
{
    CST328State *s = CST328(dev);
    *x = s->x;
    *y = s->y;
    return s->pressed;
}

void cst328_set_contact(DeviceState *dev, bool pressed, uint16_t x, uint16_t y)
{
    CST328State *s = CST328(dev);
    if (s->pressed == pressed && (!pressed || (s->x == x && s->y == y))) {
        return;
    }
    s->pressed = pressed;
    s->x = MIN(x, 239u);
    s->y = MIN(y, 319u);
    /* INT announces a new report, including movement and release. Both
     * edge polarities are observable; pulse width is not modeled. */
    if (s->int_irq) {
        qemu_set_irq(s->int_irq, 0);
        qemu_set_irq(s->int_irq, 1);
    }
}

static void class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    I2CSlaveClass *ic = I2C_SLAVE_CLASS(klass);
    ic->send = touch_send;
    ic->recv = touch_recv;
    ic->event = event;
    device_class_set_legacy_reset(dc, reset);
}

static void instance_init(Object *obj)
{
    CST328State *s = CST328(obj);
    qdev_init_gpio_out_named(DEVICE(obj), &s->int_irq, "int", 1);
}

static const TypeInfo info = {
    .name = TYPE_CST328,
    .parent = TYPE_I2C_SLAVE,
    .instance_size = sizeof(CST328State),
    .instance_init = instance_init,
    .class_init = class_init,
};

static void register_types(void)
{
    type_register_static(&info);
}
type_init(register_types)
