# Sample MARCXML — synthetic

These records are **synthetic**, hand-authored to exercise the M2 pipeline
on a known mix of valid and broken inputs while real Helmet records are
still pending (see `docs/external-dependencies.md` Ask 1). M2 is "developed
against synthetic" but **not "done"** until real records replace these.

| Filename       | Purpose                                                           |
|----------------|-------------------------------------------------------------------|
| `10000001.xml` | Finnish translation of a Russian novel — 1XX + 700 (translator).  |
| `10000002.xml` | Finnish-language original work, prose, single contributor.        |
| `10000003.xml` | Translated novel with multilingual title.                         |
| `10000004.xml` | Music score, 100 + 245 + 336 with `notated music`.                |
| `10000005.xml` | E-book, 245 + 008 marking electronic carrier.                     |
| `10000006.xml` | Serial publication, 245 + 008 leader byte 7 = `s`.                |
| `99999900.xml` | **Broken — bad encoding**: bytes are Latin-1, not UTF-8.          |
| `99999901.xml` | **Broken — XSD-failing**: top-level element is not `<collection>` or `<record>`. |
| `99999902.xml` | **Broken — minimum-content-failing**: missing 245 title.          |
