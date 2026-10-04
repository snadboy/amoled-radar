#pragma once
#include <stdbool.h>
// Over-the-air updates from the radar server (/firmware.json + /firmware.bin).
void ota_mark_good(void);   // call once the new firmware has shown a frame
bool ota_check(void);       // returns only if no update was applied
