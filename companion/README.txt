Meshtastic OTA Studio — lokalni agent

Potreban je Python 3.10 ili noviji. Prvo pokretanje automatski stvara privatni
Python okoliš i instalira Meshtastic Python biblioteku i esptool 5+ za USB pristup,
prepoznavanje uređaja te ciljani popravak OTA loadera.

Windows: dvoklik na start-windows.bat, pa zalijepite URL Pages stranice.
Linux/macOS: pokrenite ./start-linux-macos.sh https://vas-projekt.pages.dev

Ostavite prozor agenta otvoren tijekom ažuriranja. Zadano sluša samo na
127.0.0.1:8765. Ako preglednik zatraži dozvolu za pristup lokalnoj ili loopback mreži, dopusti je za domenu Pages; bez te dozvole javna HTTPS stranica ne može pozvati localhost agenta. Za identifikaciju uređaja agent se povezuje na Meshtastic TCP
port 4403; taj port mora biti dostupan s računala agenta.

Za firmware 2.7.18+ agent šalje OTA zahtjev preko Meshtastic TCP/4403; nod se
zatim ponovno pokrene u OTA loader, a agent šalje sliku nodu preko TCP/3232. Za
stariji firmware agent zatraži legacy OTA reboot pa koristi stari ESP OTA protokol.
Agent mora moći dosegnuti nod preko potrebnih portova; isključi Wi-Fi client
isolation i provjeri VLAN/firewall pravila.

Agent na drugom računalu ili serveru
-------------------------------------
Remote binding zahtijeva HTTPS certifikat, ključ i pristupni token. Na serveru,
nakon prvog pokretanja i dependency setupa, pokreni:

  .venv/bin/python meshtastic_agent.py --origin https://meshtastic-ota-updater.pages.dev --bind 0.0.0.0 --token 'DUGI-TAJNI-TOKEN-OD-NAJMANJE-24-ZNAKA' --certfile /putanja/agent.crt --keyfile /putanja/agent.key

Na Windowsu koristi .venv\Scripts\python.exe. U pregledniku unesi HTTPS URL
servera i isti token. Certifikat mora biti valjan i preglednik ga mora
vjerovati; dopusti LAN pristup u pregledniku i otvori port 8765 samo na
pouzdanom LAN-u. Ne prosljeđuj port na internet. Token se ne sprema u
localStorage, ali ostaje u polju do zatvaranja stranice.

Postavke uređaja
----------------
U web sučelju se mogu očitati, usporediti i kopirati postavke preko mrežnog
TCP-a, USB/Serial veze ili Bluetooth LE-a. USB i Bluetooth uređaji moraju
biti fizički dostupni računalu na kojem radi agent. Na Linuxu Bluetooth
zahtijeva ispravan BlueZ adapter i dozvole korisnika. Službena Meshtastic
Python biblioteka upravlja prijenosom; aplikacija radi jedan uređaj po jedan.

JSON profili spremaju se lokalno u preglednik ili ih korisnik ručno preuzima.
Ne uključuju sigurnosnu konfiguraciju ni fiksnu lokaciju. Odabrani kanali
uključuju PSK ključeve, a ostale postavke mogu sadržavati Wi-Fi/MQTT lozinke;
izvezene profile čuvajte privatno. Prije grupne primjene provjerite LoRa
regiju i frekvencijske postavke za sve uređaje.

Samostalni USB popravak
----------------------

USB popravak je odvojen od mrežnog pretraživanja i Wi-Fi firmware koraka. Spoji
node na računalo agenta, klikni Skeniraj USB i prepoznaj uređaj. Agent preko
Meshtastic serial protokola očita naziv, hardware model i firmware; sučelje
poveže model sa službenim katalogom i ne traži ručni odabir pločice ni firmware
izdanja. Ako ima više prepoznatih nodova, odaberi po automatski očitanom modelu.

Klikni Provjeri loader za čitanje ESP32-S3 partition tablice i app1 slike preko
USB-a. Ovo čitanje kratko resetira uređaj, ali ništa ne zapisuje. Agent usporedi
SHA-256 instaliranog loadera s ugrađenim popravkom 02e63d9. Ako je isti, uređaj
je već popravljen i ništa se ne zapisuje. Ako se točan hash ne podudara, alat
predloži popravak. Nakon izričite potvrde, agent ponovno pročita partition
tablicu i zapisuje loader samo u stvarnu app1 particiju tog uređaja. Provjerava
app0/app1 tip, subtype, veličine, nepreklapanje, granice flasha i SHA-256 slike. Ako Secure Boot/flash encryption nije potvrđeno isključen, odbija automatski upis.
Ako Heltec Wireless Tracker ne uđe automatski u download način, aplikacija prikazuje službeni redoslijed USER/RESET i omogućuje ponovnu provjeru u ROM načinu bez resetiranja između provjera. Glavni firmware app0 i postavke ostaju netaknuti. Drugi serijski programi moraju
biti zatvoreni. Binarij je lokalno kompajliran iz Meshtastic esp32-unified-ota
commita 02e63d9; nije službeno označeno izdanje. Opće firmware flashanje preko
USB-a/Bluetootha nije podržano.


Update note: use companion agent v0.4.0 or newer. It adds USB identity and partition/loader inspection for the standalone repair, fixes protobuf config reads, registers POST requests, and uses official Unified OTA for firmware 2.7.18+ with legacy fallback.
If an ESP32-S3 node closes the TCP/3232 connection after the image header and
reboots, its older Wi-Fi OTA loader may need a one-time USB update before OTA
can work again. Do not repeatedly retry if the agent reports a loader reset.
