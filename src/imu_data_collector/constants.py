"""采集产物与 CW12EU-T 设备档案共用的常量。"""

CAPTURE_SCHEMA_NAME = "imu_capture_hdf5"
CAPTURE_SCHEMA_VERSION = "1.0.0"
STANDARD_GRAVITY_MPS2 = 9.80665

FEATURE_COLUMNS = (
    "acceleration_x_mps2",
    "acceleration_y_mps2",
    "acceleration_z_mps2",
    "angular_velocity_x_rad_s",
    "angular_velocity_y_rad_s",
    "angular_velocity_z_rad_s",
)
FEATURE_UNITS = ("m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s")

CW12EU_NOTIFY_UUID = "00002ae1-0000-1000-8000-00805f9b34fb"
CW12EU_FRAME_BYTES = 16
