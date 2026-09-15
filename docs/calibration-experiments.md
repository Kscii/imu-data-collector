# Guided IMU calibration experiments

Use this workflow to estimate conversion coefficients for the unchanged
`IMU-0002-R01` (`acce&gyro`) device. The result is an **unverified candidate**.
Keep the device, firmware and mounting conditions consistent throughout an experiment.

## Install and open

Download the matching installer and its SHA-256 file from the
[desktop preview release](https://github.com/Kscii/imu-data-collector/releases/tag/desktop-v0.3.1-rc.2):

- Windows 10/11 x64: the `.exe` installer. This internal installer is unsigned;
  Windows may show SmartScreen. Check the release source and hash before opening it.
- macOS 13+, Apple Silicon: the `arm64` DMG.
- macOS 13+, Intel: the `x86_64` DMG.

On macOS, drag the app into Applications. The app is ad-hoc signed and is not
notarized. If macOS blocks this trusted copy, use **System Settings → Privacy &
Security → Open Anyway** after attempting to open it, as described in
[Apple's instructions](https://support.apple.com/en-gb/102445). Allow Bluetooth
access when prompted. The calibration workflow does not require video recording.

Open **Devices & settings**, select the device, and open the calibration tool.
Close other programs connected to the same IMU. Only one computer should collect
from the device at a time.

## Define the physical directions

Create an experiment and select the actual operator. Describe all six housing
directions: **+X, −X, +Y, −Y, +Z, −Z**. Use observable features, such as the
connector face. Do not copy the old device's button or pendant directions.

Choose a right-handed coordinate system. For each static trial, point the selected
signed direction straight upward. Positive rotation follows the right-hand rule:
point your right thumb along the positive axis; your curled fingers show positive
rotation. Negative rotation goes the other way.

## Run the trials

Connect the device, then select the next suggested trial. The suggestions are
minimums, not limits. Each repetition is a separate start-to-finish trial.

| Trial | Minimum per axis and direction | Procedure |
| --- | --- | --- |
| Static acceleration, fit | 3 | Hold the selected face upward: 5 s settling, then 10 s measurement. |
| Static acceleration, validation | 1 | Use the same procedure with separate data. |
| Gyroscope, fit | 5 | 5 s still, rotate 360°, press Finish rotation, then remain still for 5 s. |
| Gyroscope, validation | 2 | Use separate 720° rotations with the same stationary windows. |

The minimum is 24 static trials and 42 rotation trials. After the first six static
fitting trials, use **Preview results / check axis mapping** to review the inferred
raw channels and signs. Check these against your housing descriptions.

Rotate around the selected axis through the device centre. Use a fixture or clear
angle marks to check the endpoint and number of turns. Keep the axis steady.
Exactly constant speed is not required; avoid sudden knocks and sensor saturation.
There is no offset-radius or centripetal-acceleration experiment in this version.

The server controls phase timing. Watch the on-screen countdown. Between trials,
rest or reposition the device. To stop for longer, finish the capture and later
continue the same experiment; the new capture creates another raw H5 file.
An interrupted trial remains recorded and can be repeated. Excluding a trial
requires a reason and preserves its raw evidence.

## Read and save the result

Finish the capture and select **Calculate and save report**. The report contains
every trial, the inferred mapping, candidate scales and biases, per-axis diagnostic
estimates, validation errors and warnings. Each changed report gets a new immutable
version. Old reports remain available.

The accelerometer estimate uses medians from positive and negative gravity poses.
The gyro estimate subtracts the trial's stationary bias, integrates raw counts
using device timestamps, and divides by the known rotation angle. Both retain a
single shared scale across the three axes. Raw-axis biases are applied before
reordering and applying signs.

Independent validation uses the final fixed candidate scales **and fixed fitted
biases**. It does not re-zero itself from validation data. Adding fitting trials
produces a new candidate and re-evaluates the separate validation trials; their
data never enters the fitting calculation.

`counts/g` converts to acceleration using 9.80665 m/s² per g.
`counts/(°/s)` converts to angular velocity using π/180 radians per degree.
The UI reports differences and warnings, without an automatic accuracy pass/fail
gate. Missing or invalid data produces missing calculations, not invented values.
The number of repetitions alone does not establish accuracy.

Download candidate YAML or copy it into the local configuration workspace. The
six housing descriptions and report hash accompany the candidate. It stays
`verified=false`, with only `test` data allowed. Existing configuration review and
activation remain explicit later actions.

## Share with the team

Sign in using the existing team Google account and select **Upload experiment**.
The desktop sends raw H5 files and one report JSON through the upload broker.
After verifying byte sizes and SHA-256 hashes, the broker publishes the manifest.
The signed-in uploader and the experiment operator are recorded separately.

In the annotation website, open **Device configuration → Calibration experiments**
to review reports, warnings and individual results, or download raw files.
There is no cloud recalculation or raw waveform browser. Collection and analysis
work offline; failed uploads can be retried.

## Evidence format and compatibility

Raw capture retains the existing HDF5 schema. The separate JSON schema is
`imu_calibration_experiment_v1`. An experiment has stable trial IDs and can reference
multiple raw files. Report versions use SHA-256 identifiers. Local paths are omitted
from shared reports. Old characterization reports remain readable.

The broker adds `/v1/calibration-uploads` and `/v1/calibration-uploads/complete`.
The desktop and annotation website expose `/api/v1/calibration-experiments` with
their respective local workflow and cloud catalog operations. Objects live under
`calibration-experiments/v1/<sensor_sn>/<experiment_id>/<report_sha256>/`.
Only a completed manifest makes a report visible in the team catalog.

Methods: [ADI AN-1057](https://www.analog.com/en/resources/app-notes/an-1057.html)
and [A Simple Calibration for MEMS Gyros](https://www.analog.com/media/en/technical-documentation/technical-articles/GyroCalibration_EDN_EU_7_2010.pdf).

## Hardware acceptance record

Automated tests use simulated data and do not establish physical sensor accuracy.
For each target OS, record installer version, host/OS, BLE connection, six-channel
sample reception, completed raw H5, report generation and upload/download results
when a teammate performs the first hardware experiment. Windows and both macOS
architectures require that physical check; no such result is claimed by this guide.
