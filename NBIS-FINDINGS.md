# Does NBIS (mindtct + bozorth3) work on a 144x40 SYNA8002 image?

**No — and the limit is sensor area, not image quality.**

Method: NBIS 5.0.0 `get_minutiae()` called directly on a raw 8-bit buffer with an
explicit pixels-per-mm, which is how libfprint uses its bundled NBIS. Matching with
stock `bozorth3`. Control set: FVC2000 DB1 prints at 500 dpi.

## Geometry of this sensor

Ridge period measured from the 2-D power spectrum of 15 real captures, on a
square-pixel grid (rows doubled, since 80 lines de-interleave to 40 rows):
**7.45 px/ridge**. At a human ridge pitch of ~0.5 mm that puts the 144x40 readout at
**~9.7 x 5.4 mm, about 29 mm^2** of finger.

## Control — the harness is correct

| image | full frame | minutiae | cropped to 29 mm^2 | minutiae |
|---|---|---|---|---|
| 101_1 | 245x372 | 33 | 200x55 | 4 |
| 101_2 | 388x374 | 39 | 200x55 | 2 |
| 102_1 | 388x374 | 62 | 200x55 | 5 |
| 102_2 | 388x374 | 50 | 200x55 | 0 |

bozorth3 on the full frames: genuine **53** and **194**, impostors **6-8**. Textbook.
bozorth3 on the same prints cropped to this sensor's field of view: **0 for every
pair, genuine included.**

That is the whole answer. A flawless 500 dpi print, cropped to the area this sensor
sees, is already unmatchable by bozorth3.

## Real captures

mindtct is not empty — it finds a median of 8-12 minutiae per capture (raw,
contrast-normalised, and Gabor-enhanced variants all tested, 35 captures).
But they do not match:

| preprocessing | genuine (n=120) | impostor (n=180) | genuine >= 20 |
|---|---|---|---|
| raw | median 0, max 16 | median 0, max 8 | 0/120 |
| contrast | median 0, max 4 | median 0, max 7 | 0/120 |
| gabor | median 0, max 0 | median 0, max 0 | 0/120 |

No separation at any threshold. Note the impostor captures often yield *more*
minutiae than genuine ones (imp2index: 15-31), which says those minutiae are
substantially noise, not features.

## How big would the sensor have to be?

Same perfect 500 dpi prints, centre-cropped to progressively larger windows:

| window | area mm^2 | median minutiae | genuine scores | impostor scores |
|---|---|---|---|---|
| 10.2 x 2.84 | 29 | 3 | 0, 0 | 0, 0, 0, 0 |
| 12 x 4 | 48 | 8 | 0, 0 | 0, 0, 0, 0 |
| 14 x 6 | 84 | 14 | 15, 0 | 0, 0, 0, 0 |
| 16 x 8 | 128 | 20 | 36, 6 | 3, 3, 3, 0 |
| 18 x 10 | 180 | 25 | 52, 41 | 5, 5, 3, 3 |
| 18 x 14 | 252 | 37 | 129, 50 | 8, 6, 5, 4 |

Usable separation appears around **130-180 mm^2**. This sensor sees **29 mm^2**,
roughly six times too little.

## Consequence for a libfprint driver

A driver that hands up a 144x40 image and relies on NBIS will enrol and then never
match. Frame stacking does not rescue it: consecutive frames are temporal repeats
(they differ by ~1.4, the noise floor), so they average for SNR but cannot be
mosaicked into a taller image the way a swipe sensor's frames can.

So the image-size question is not "is quality a separate concern" — for this part it
decides whether the device works at all. Either the driver carries a matcher suited
to small-area sensors (correlation-based, as `synaspi` does), or the device needs to
be driven as match-on-chip, or it does not work.
