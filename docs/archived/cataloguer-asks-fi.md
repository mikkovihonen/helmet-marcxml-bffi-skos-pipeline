# Pyynnöt Helmet-luetteloijalle

> **Archived.** Finnish-language cataloguer-facing copy of the requests
> originally listed in `docs/external-dependencies.md`. Retained for reference;
> the live English source remains at
> [`../external-dependencies.md`](../external-dependencies.md). If a fresh
> Finnish version is needed for re-distribution, regenerate it from the live
> document rather than editing this snapshot.

---

Tämä dokumentti sisältää suomenkieliset versiot pyynnöistä, jotka ovat määriteltyinä englanniksi pääprojektin `CLAUDE.md`-tiedostossa kohdassa "External dependencies — records to request from Helmet cataloguers". Tämä versio on tarkoitettu välitettäväksi suoraan Helmet-luetteloijalle.

**Konteksti:** Rakennan pro bono -työnä työkalua, joka muuntaa Helmetin bibliografiset MARCXML-tietueet linkitetyksi dataksi suomalaisen BIBFRAME-tietomallin (BFFI) mukaisesti, ryhmittelee ne RDA-teoksiksi ja julkaisee tulokset Skosmos-pohjaisena selauspalveluna. Hanke on tarkoitus luovuttaa Kansalliskirjastolle valmistuttuaan. Osa kehitysvaiheista vaatii oikeita Helmet-tietueita, koska synteettinen testidata ei tuota uskottavia tuloksia.

Pyyntöjä on neljä, ja ne porrastuvat kehityksen edetessä. Pyynnöt 1 ja 3 tarvitaan kehityksen alkuvaiheessa ennen tietueiden sulautuksen aloittamista, pyyntö 2 ennen auktoriteettiyhdistämisen kehittämistä, ja pyyntö 4 ennen tuotantojulkaisua.

---

## Pyyntö 1 — Kuratoitu kehitysotos (~15 tietuetta)

Käsin valitut tietueet, jotka yhdessä kattavat ne tapaukset, joita putki käsittelee. Kustakin tarvitaan kolme asiaa:

1. **Helmet-tietuetunnus** (tiedostonimen muodossa `<tunnus>.xml`).
2. **Yhden lauseen perustelu** siitä, mitä tietue havainnollistaa.
3. **Odotettu lopputulos selkokielellä** — esimerkiksi "tulisi yhdistyä tietueen X kanssa samaksi RDA-teokseksi" tai "tulisi pysyä erillisenä tietueesta Y identtisestä nimekkeestä huolimatta".

Odotetut lopputulokset toimivat samalla työkalun kultaisen otoksen siemenenä laadunvalvontaa varten.

Pyydettävät tapaukset:

### Teos- ja ekspressiotapaukset (sulautuslogiikan kannalta)

1. Yksinkertainen suomenkielinen alkuperäisteos, monografia, yksi tekijä.
2. Sama suomenkielinen alkuperäisteos ruotsiksi käännettynä — eri Helmet-tietue, sama RDA-teos.
3. Venäjänkielinen alkuperäisteos suomeksi käännettynä (translitterointi + käännös).
4. Englanninkielinen alkuperäisteos suomeksi käännettynä (eri translitterointiprofiili kuin venäjässä).
5. **Yleisen nimekkeen törmäys** — kaksi saman suomalaisen tekijän tietuetta, joilla on sama yleinen nimeke mutta jotka ovat eri teoksia (esim. varhainen runokokoelma "Runot" vs. postuumi valikoima samalla nimellä).
6. Romaanin sovitus elokuvakäsikirjoitukseksi tai sarjakuvaromaaniksi (sama lähde, eri sisältötyyppi).
7. Pitkemmän teoksen lyhennelmä (vaikein "eri teos" -tapaus).

### Aineistotyyppikirjo (BFFI:n alaluokkien testaamista varten)

8. Musiikkiäänite (ääni).
9. Eri musiikkiteoksen nuotti tai partituuri (notatoitu musiikki).
10. Karttamateriaali.
11. Sarjajulkaisu / jatkuva julkaisu (valinnainen, mutta hyödyllinen jos tuotantoaineisto sisältää sarjajulkaisuja).

### Reunatapaukset

12. Tietue, jonka tekijä on yhteisö (esim. virastojulkaisu, yhdistyksen vuosikertomus).
13. Tietue, jossa on useita tasavertaisia tekijöitä eikä selvää pääasiallista tekijää.
14. Kokoomateos tai antologia, jossa on omaperäisiä osatekijöitä.
15. **Tarkoituksellisesti ongelmallinen tietue** — sellainen, joka on jostain syystä mielestäsi vähän kiusallinen: luetteloinnin erikoispiirteitä, puuttuvia kenttiä, koodausvirheitä tms. Stressitestaa työkalun validointivaiheen.

---

## Pyyntö 2 — Auktoriteettiyhdistämisen siemenotos (~15 tietuetta)

Tietueet, joiden avulla kehitetään ja testataan tekijöiden ja aiheiden yhdistämistä auktoriteettilähteisiin (KANTO, VIAF, YSO, KAUNO, MUSO):

- **5–10 tietuetta**, joiden tekijät löytyvät KANTOsta auktorisoiduissa muodoissaan. Onnistuneen polun testaaminen.
- **3–5 tietuetta**, joiden tekijöitä ei löydy KANTOsta — tyypillisesti ulkomaisia kirjoittajia, joiden teoksia Helmetissä kuitenkin on. VIAF-varapolun testaaminen.
- **3–5 tietuetta**, joissa MARC-otsikko poikkeaa KANTOn auktorisoidusta muodosta: vaihteleva translitterointi, eri syntymä-/kuolinpäivät, vaihtoehtoinen kirjoitusasu. Nämä ovat tapauksia, joissa kielimallin tekemä valinta tuo lisäarvoa pelkkään tekstivertailuun nähden.
- **Muutama tietue**, jossa on YSO-asiasanoja 650-kentässä, mieluiten sekä `$2 yso/fin` että `$2 yso/swe` -muodoissa.

---

## Pyyntö 3 — Aineiston kuvailu (tilastoja, ei tietueita)

Tilastollisia yhteenvetoja tuotantoaineistosta, ei tietueita. Tarvitaan ennen tuotantomittakaavaista ajoa:

- **Tietueiden kokonaismäärä** (oletus 800 000; vahvistus).
- **Aineistotyyppien jakauma** (monografia / musiikkiäänite / AV / kartta / sarjajulkaisu / muu).
- **Kielten jakauma**.
- **Arvio käännösten ja alkuperäisteosten suhteesta**.
- **Yksittäinen vienti vai inkrementaaliset päivitykset?** (Vaikuttaa siihen, kuinka työkalun tulee käsitellä päivityksiä.)
- **Onko tietueita, jotka tulee jättää pois** tuotantoajosta? (Esim. väliaikaiset tietueet, poistoa odottavat, luettelointivirheellisiksi merkityt.)
- **Onko tietueita, joiden uudelleenjulkaisuun linkitettynä avoimena datana liittyy rajoituksia** (esimerkiksi tekijänoikeudellisia tai käytäntöperusteisia)?

---

## Pyyntö 4 — Käytäntövahvistus (ennen tuotantojulkaisua)

Ennen julkaisua julkiseen Skosmos-instanssiin tarvitaan Helmet-kirjastokimpan eksplisiittinen vahvistus seuraavasta:

- **Bibliografinen metadata** voidaan julkaista uudelleen avoimena linkitettynä datana.
- Valittu **URN-nimiavaruus** `http://urn.fi/URN:NBN:fi:bib:work:` on hyväksyttävä ja Kansalliskirjaston kanssa yhteensovitettu.
- Mikään tietty tietue tai tietueluokka ei vaadi poissulkemista tuotantojulkaisusta.
- Julkaistun **RDF-datan lisenssi** on ratkaistu (todennäköisesti CC0 Finto-konvention mukaisesti; vahvistettava).

Tämä on käytäntö-, ei teknistä asiaa, joten sen läpivienti voi viedä viikkoja sähköpostia. Suosittelen aloittamaan tämän pyynnön epävirallisesti hyvissä ajoin ennen suunniteltua julkaisuhetkeä, jotta vahvistus ehtii valmistua.

---

## Saateteksti luetteloijalle (Pyyntö 1)

Tätä voi käyttää sähköpostin runkona ensimmäistä yhteydenottoa varten:

> Hei,
>
> Rakennan pro bono -työnä työkalua, joka muuntaa Helmetin bibliografiset tietueet linkitetyksi dataksi ja ryhmittelee ne RDA-teosten mukaan. Hanke on tarkoitus luovuttaa Kansalliskirjastolle valmistuttuaan.
>
> Testaamista varten tarvitsen kuratoidun joukon noin 15:tä tietuetta, jotka kattavat tiettyjä tapauksia — käännöksiä, sovituksia, samannimisten teosten törmäyksiä, musiikkiäänitteitä vs. partituureja, yhteisötekijöitä ja muutaman tietueen, joissa on tunnettuja luetteloinnin erikoispiirteitä. Kustakin tietueesta tarvitsen Helmet-tietuetunnuksen, yhden lauseen perustelun siitä, miksi se on kiinnostava, ja lyhyen kuvauksen odotetusta lopputuloksesta (esim. "tulisi yhdistyä tietueen X kanssa samaksi teokseksi").
>
> Olen laatinut tarvittavien tapausten listan oheen — voisitko ehdottaa sopivia Helmet-tietuetunnuksia kuhunkin kohtaan? Tunnet luettelon huomattavasti paremmin kuin minä. Arvioin tehtävän vievän noin puoli tuntia työaikaasi.
>
> Kiitos jo etukäteen!
>
> Ystävällisin terveisin,
> [oma nimi]
>
> ---
>
> [Liitteenä Pyyntö 1:n lista yllä olevasta]

Pyynnöt 2–4 voi lähettää myöhemmin omina viesteinään, kun kehitys etenee niitä vaativiin vaiheisiin.
