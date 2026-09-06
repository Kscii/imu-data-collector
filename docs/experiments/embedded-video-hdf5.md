# Embedded MP4 in HDF5 experiment

Date: 2026-09-04

Decision: adopted by the HDF5 3.2 `client_delivery` profile after full-size validation

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

A later local experiment converted the immutable 11-recording snapshot
`snapshot-84f6cd83ce754f9cd5cd9e49` and produced these measurements:

| Item | Result |
| --- | ---: |
| Source package | 2,842,101,659 bytes |
| Single HDF5 | 2,839,336,957 bytes |
| Core HDF5 plus video payload | 2,837,495,521 bytes |
| HDF5 overhead over net payload | 0.0649% |
| Recordings / videos | 11 / 11 |
| Experimental HDF5 SHA-256 | `a8a8314ae8dfdf6f4dc0a6d3e3ceb3ceaba2e4d7560e14b495e47318eba79b24` |

This digest identifies the historical experiment only; production 3.2 delivery
is regenerated from immutable source objects and receives a new digest.

## Product decision

The later full-size experiment used the existing 11-recording, approximately
2.84 GB snapshot and confirmed that the same physical-range technique works for
all 11 videos with low container overhead. The layout is therefore adopted by
the HDF5 3.2 `client_delivery` profile. The strict `training_dataset` profile
continues to exclude media.

The trade-offs remain real: regeneration rewrites the large file, ordinary HDF5
tools expose video as bytes rather than a media player, and any repack or in-place
mutation invalidates the frozen physical offsets. Production construction must
therefore close and fully validate the file, verify every MP4 by physical range,
publish it immutably, and never run `h5repack` on the result.

Cross-browser and cross-platform acceptance of the final production-generated
artifact remains a release gate; the synthetic experiment alone is not that
acceptance evidence.
