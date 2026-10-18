# CW12EU-T coordinate system and calibration

This engineering profile applies only to the physical device originally identified
by BLE address `83:FC:90:14:1E:A4`, profile
`cw12eu_t_83fc90141ea4_v1_20260827`. It is retained evidence, not proof that the
original unit is still available. Similar-looking devices cannot inherit it.

The `engineering_verified` status records consistency across static faces and
manual rotations. It is not a traceable metrology calibration or identification
of the internal MEMS component.

## Right-handed sensor coordinates

| Direction | Housing direction |
| --- | --- |
| +X | Wearer's right; opposite the interface face |
| -X | Interface face |
| +Y | Toward the head; pendant/lanyard end |
| -Y | Opposite the pendant end |
| +Z | Away from the body; button face |
| -Z | Toward the body; charging/button-back face |

A positive target axis pointing upward gives approximately +1 g on that axis.
Both sensors use the same raw column order and sign mapping `[raw_1, -raw_2, raw_3]`.
A clockwise description is ambiguous without an observation direction; check
response axes and sign reversal against physical orientation.

## Conversion

```text
accel_bias_counts_raw = [30, 48, -40]
gyro_bias_counts_raw = [-17, 0, 10]
raw_axis_order = [0, 1, 2]
axis_signs = [1, -1, 1]
accel_counts_per_g = 4096
gyro_counts_per_dps = 32.8

corrected = (raw - bias_raw)[raw_axis_order] * axis_signs
acceleration_mps2 = corrected_accel / 4096 * 9.80665
angular_velocity_rad_s = corrected_gyro / 32.8 * pi / 180
```

Subtract biases in raw-axis space before reordering/sign changes. Raw counts never
change. Derived SI values use the profile and evidence frozen into each recording.

## Evidence

[calibration-evidence.yaml](../configs/calibration-evidence.yaml) references six
static face trials, eight approximately 360-degree rotations, and one short
free-fall trial. Static half-spans were approximately 4086, 4089, and 4089 counts/g;
rotations integrated to about 354.5–370.6 degrees using 32.8 counts/(degree/s).
Manual angle, endpoint, and off-axis errors limit significant figures.

The calibration-evidence page reads these artifacts without making them ordinary
annotation tasks or training examples. New devices, changed firmware/orientation,
conflicting measurements, better instruments, or material drift require new profile
evidence. Preserve old profiles and recordings; do not silently change their units.
For new measurements use [Calibration experiments](calibration-experiments.md).
