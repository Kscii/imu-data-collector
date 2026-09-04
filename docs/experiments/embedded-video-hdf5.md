# Embedded MP4 in HDF5 experiment

Date: 2026-09-04

Decision: technically viable, rejected for the production customer-delivery contract

## Question

Can an MP4 be embedded as a contiguous HDF5 byte dataset and then played by a
browser without loading or copying the complete HDF5 file into JavaScript
memory?

## Method

A two-second H.264/YUV420p MP4 was generated as non-identifying synthetic test
media. It was written to a contiguous, uncompressed `uint8` dataset at
`/media/video_mp4`. The dataset froze its content type, length, SHA-256 and
physical file byte offset as attributes. The HDF5 file was closed before the
range was read back.

The byte range was checked twice:

1. a native file seek/read compared the range with the source MP4; and
2. Chromium selected the HDF5 as a local `File`, used `File.slice(offset,
   offset + size)`, and calculated the sliced bytes' SHA-256 with Web Crypto.

## Result

```text
HDF5 size:    13,564 bytes
MP4 size:      3,324 bytes
MP4 offset:    6,144 bytes
SHA-256: e7afc3afed465a837c343164e6e842622d7e7e819218a359dba7cc4c3c13d44b
```

The native range and Chromium slice both returned exactly 3,324 bytes with the
same SHA-256 as the source MP4. The narrow byte-range technique therefore works
when the media dataset is contiguous, unfiltered and its immutable physical
offset is frozen after the file is finalized.

## Product decision

This layout is not adopted for customer delivery. It couples large identifying
media to a scientific data container, makes any HDF5 rewrite copy the video,
reduces compatibility with ordinary HDF5 tooling, and enlarges the failure
scope of one corrupt file. A physical offset is also a storage-layout property,
not a normal portable HDF5 semantic contract.

The production v2 delivery remains a ZIP64/`ZIP_STORED` package containing one
unchanged `cw12eu.h5`, independent MP4 files and explicit `view.json` mappings.
This retains direct browser slicing while keeping training HDF5 independent of
video. No embedded-video prototype code or branch is retained.

## Remaining limitation

This experiment established format feasibility with synthetic media, not
performance on a complete customer package. Large-package acceptance belongs
to the ZIP v2 viewer tests and must use an authorised real delivery artifact.
