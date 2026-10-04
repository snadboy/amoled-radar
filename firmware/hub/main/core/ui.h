#pragma once
#include <stdbool.h>
#include "lvgl.h"

// LVGL on top of the board: renders into the board's two DMA strip buffers, reads
// touch, and runs in its own task. Hold ui_lock() around every lv_* call made from
// another task. ui_pause(true) stops rendering (screen off, or an app drawing to the
// panel directly); resuming redraws the whole screen.
void ui_init(void);
bool ui_lock(int timeout_ms);       // 0 = wait forever
void ui_unlock(void);
void ui_pause(bool paused);
