# CW12EU-T protocol evidence

This reference preserves observations of the original CW12EU-T unit. Evidence
labels distinguish verified observations, supplier claims, and working assumptions.
The registry links to this path; its existence is not a claim of current hardware
availability or support for all devices sold under the same name.

| Property | Observation | Evidence boundary |
| --- | --- | --- |
| Advertised name | `CW12EU-T` | Observed; not a unique identity |
| BLE address | `83:FC:90:14:1E:A4` | Original physical unit only |
| Data notification | `0x2AE1` under service `0x18E0` | Verified with Bleak |
| Sample payload | 400 bytes, 25 frames of 16 bytes | Verified on this unit |
| Frame | Six big-endian int16 counts plus four trailer bytes | Six-axis interpretation verified; trailer meaning unknown |
| Sampling | Approximately 25 Hz in probes and two ten-minute static runs | Supplier claimed 30 Hz; no 30-minute acceptance claim |
| Acceleration | `[raw_ax, -raw_ay, raw_az]`, 4096 counts/g | Six-face engineering calibration |
| Angular velocity | Same mapping, 32.8 counts/(degree/s) | Multi-axis manual rotation evidence |
| Radio platform | Evidence suggests JL701N | Does not identify board vendor, MEMS, or firmware author |

The historical reference PDF had SHA-256
`ad214ce70b7b89253886615675a3447d621625134437394364ded8008f61ddc8`.
It is not an authoritative protocol specification for every similarly named device.

## Connection behavior

The device exposes both classic Bluetooth and BLE. A generic BlueZ connection
can choose BR/EDR and expose only classic services. The exercised Linux path sets
`PreferredBearer=le`, connects through the LE bearer, and then subscribes to GATT.
The observed unit did not require bonding. `0x2AE1` is shown as a standard voltage
UUID by some tools, but this device uses it as a vendor data channel; the registered
UUID name does not explain its payload.

A 15.0016-second probe received 15 notifications of 400 bytes: 375 sample frames,
24.99979 Hz estimated, 999.986 ms median packet interval, and no parser errors or
callback drops. These are historical measurements, not general device guarantees.
The GATT tree also exposed `0x2AE0`/`0x2AE2`, service `0xAE00` with `0xAE01`/`0xAE02`,
and `0x180D` with `0x2A37`; undocumented payloads remain raw evidence.

Use [Device integration](device-registry-and-protocols.md) for onboarding and
[Coordinate system](device-coordinate-system.md) for the exact calibration profile.
Do not apply these coefficients to the separate ABF2 device or a replacement unit.
