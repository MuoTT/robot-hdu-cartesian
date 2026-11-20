# Periodic state of the plotter primitive.
bool   connected
bool   busy
string last_error

# Last known / commanded position, mm.
float64 x
float64 y
float64 z

# Current pen heights, mm.
float64 z_up
float64 z_down

# IDLE | MOVING | HOMING | ERROR | DISCONNECTED
string state
# Monotonic counter for this stream.
uint64 seq