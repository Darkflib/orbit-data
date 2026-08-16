# Data sources and attribution

`orbit-data` normalizes the sources below for use by
[Darkflib/orbit](https://github.com/Darkflib/orbit). Satellite enrichment files
record the winning source for each field in their `_sources` object.

- **CelesTrak GP element sets** — T.S. Kelso, [celestrak.org](https://celestrak.org/),
  used with attribution. Fetched from `https://celestrak.org/NORAD/elements/gp.php`
  as `FORMAT=CSV` and republished under `/v1/gp/` as OMM JSON. The CSV rendering
  is about a third the size of the JSON one and `gp.php` serves no compression,
  so the conversion happens here rather than being asked of CelesTrak; the
  published records are the ones `FORMAT=JSON` would have returned. One
  `GROUP=stations` response — 22 objects, ~13 KB across both formats — is
  committed under `tests/fixtures/` and used to prove that claim rather than
  assert it. It is CelesTrak's data, redistributed under the same attribution
  as everything else here, and captured once rather than fetched by the suite,
  so the tests place no load on their service.
- **CelesTrak SATCAT** — T.S. Kelso, [celestrak.org](https://celestrak.org/),
  used with attribution. Fetched from `https://celestrak.org/pub/satcat.csv`. It
  is the only source that says why an object has no element set, and the only
  one that describes the orbit of an object whose elements are withheld, so
  `dataStatus`, `orbitCenter` and `approximateOrbit` come from it alone.
- **GCAT** — General Catalog of Artificial Space Objects, © Jonathan McDowell,
  [planet4589.org/space/gcat](https://planet4589.org/space/gcat/), licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Fetched from the
  published `satcat.tsv` catalogue.
- **Quicksat standard magnitudes** — © Mike McCants, distributed as freeware
  with no distribution restrictions. `vendor/qs.mag` is the preserved 2020
  catalogue previously used by Orbit because the former upstream download is
  no longer available.
- **Yale Bright Star Catalogue, 5th ed.** — D. Hoffleit and W.H. Warren Jr.,
  public-domain scientific catalogue via
  [VizieR V/50](https://cdsarc.cds.unistra.fr/viz-bin/cat/V/50). Proper names in
  `vendor/bsc5-names.json` are derived from the IAU Working Group on Star Names
  catalogue under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **Constellation figure lines** — © Olaf Frohn, from
  [d3-celestial](https://github.com/ofrohn/d3-celestial), BSD-3-Clause. The
  upstream licence notice is preserved in `vendor/constellation-lines.LICENSE`.

The vendored files were copied byte-for-byte from the existing Orbit enrichment
pipeline when this service replaced that pipeline. They are intentionally
versioned: the catalogue job does not add avoidable network dependencies for
static or unavailable sources.
