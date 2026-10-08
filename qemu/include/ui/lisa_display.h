/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef UI_LISA_DISPLAY_H
#define UI_LISA_DISPLAY_H
#include "ui/console.h"
/* Generic host display listener; it has no access to guest RAM or board pins. */
void lisa_display_init(QemuConsole *console, const char *directory);
#endif
