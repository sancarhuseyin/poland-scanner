# home-scanner

OLX.pl ilanlarını seçtiğin filtrelere göre tarayan yerel bir Python aracıdır. Web arayüzü, harita görünümü, yeni ilan hafızası ve bildirim desteği içerir. Şu an iki kategori desteklenir: kiralık ev ve araba.

## Özellikler

- Web arayüzüyle şehir, ilçe/semt, fiyat, metrekare, oda, eşya, yıl, kilometre ve anahtar kelime filtreleri.
- Adres çevresi filtresi ve Leaflet/OpenStreetMap harita görünümü.
- Yeni ilan takibi için `seen.json` tabanlı yerel hafıza.
- Favori ilanlar için tarayıcı localStorage kaydı.
- Terminal, ses, Telegram, ntfy ve özel komut bildirimleri.
- CLI ile tek seferlik tarama, periyodik izleme ve URL üretme.

## Kurulum

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python home_scanner.py init --config config.json
```

`config.json` kişisel ayar dosyasıdır ve Git'e eklenmez. Başlangıç değerleri için [config.example.json](./config.example.json) kullanılır.

## Web Arayüzü

```powershell
python home_scanner.py --config config.json serve
```

Tarayıcıda aç:

```text
http://localhost:8000
```

Arayüzde URL girmen gerekmez. Şehir ve ilçe/semt listeden seçilir. Fiyat, oda, metrekare, eşyalı/eşyasız, anahtar kelime ve hariç kelime filtreleri formdan verilebilir.

Adres çevresi filtresi için adresi yaz, yarıçapı km olarak gir ve `Adresi bul` düğmesine bas. Harita arama merkezini, yarıçapı ve ilanların yaklaşık OLX konumlarını gösterir.

`Eski ilanları gizle` açıkken tarama yalnızca daha önce görülmeyen ilanları listeler. Kapalıyken tüm eşleşmeleri gösterir; yine de hafızayı günceller.

## CLI Kullanımı

Tek seferlik tarama:

```powershell
python home_scanner.py --config config.json scan
```

İlk çalıştırmada mevcut ilanlar `seen.json` içine kaydedilir ve varsayılan olarak bildirim gönderilmez. Mevcut tüm sonuçları bildirim olarak göndermek için:

```powershell
python home_scanner.py --config config.json scan --notify-current
```

Periyodik izleme:

```powershell
python home_scanner.py --config config.json watch
```

Program çalışırken hemen tarama yapmak için terminale `tara`, çıkmak için `q` yaz.

Config'ten üretilen OLX URL'sini görmek için:

```powershell
python home_scanner.py --config config.json url
```

Eski `python olx_rent_watcher.py ...` komutları geriye dönük uyumluluk için çalışmaya devam eder.

## Bildirimler

Varsayılan bildirim terminal çıktısı ve kısa sestir. Telegram için `notifications.telegram.enabled` değerini `true` yap ve `bot_token` ile `chat_id` doldur. Değerleri config'e yazmak istemezsen ortam değişkenleri de kullanılabilir:

```powershell
$env:TELEGRAM_BOT_TOKEN="123:abc"
$env:TELEGRAM_CHAT_ID="123456"
python home_scanner.py --config config.json watch
```

ntfy için `notifications.ntfy.enabled` değerini `true` yap ve benzersiz bir `topic` belirle.

## Geliştirme

Geliştirme bağımlılıkları:

```powershell
python -m pip install -r requirements-dev.txt
```

Testler:

```powershell
python -m pytest
python -m py_compile home_scanner.py olx_rent_watcher.py
```

## GitHub'a Yüklemeden Önce

- `config.json`, `seen.json`, `translations.json`, loglar, `.env` ve geçici sunucu dosyaları `.gitignore` içindedir.
- Gizli Telegram token'larını config yerine ortam değişkenlerinde tutman önerilir. Örnek için [.env.example](./.env.example) var.

## Lisans

Bu proje [MIT License](./LICENSE) ile lisanslanmıştır.

## Notlar

- Araç OLX'in herkese açık arama sayfasından API parametrelerini çıkarır ve `api/v1/offers` endpoint'ini kullanır.
- Aralıkları makul tut: varsayılan 10 dakika, minimum pratik olarak 1 dakika.
- Login, CAPTCHA aşma veya yüksek hızlı istek davranışı yoktur.
